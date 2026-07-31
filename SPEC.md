# SPEC — the thin loop

v1.0 (2026-07-31). The buildable specification for Werft's first milestone. [README.md](README.md) doctrine governs; where this document conflicts with it, the doctrine wins and this document has a bug. Decisions behind this document: [the realignment design](docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md). Deep analysis this document compresses: [`docs/lineage/`](docs/lineage/README.md) — cited here as `[A§n]` (ARCHITECTURE-v1.4), `[BP§n]` (BUILD-PLAN-v1.0), `[CD§n]` (containment-design), `[CLP§n]` (core-loop-proof).

**Scale target:** one operator (Ken), one dedicated Rocky Linux 10 VM, single-digit projects, tens of runs per day. Every component justifies its operational cost to exactly one person.

**The milestone:** the greenfield Elastic log-analysis project goes from empty repository to its first oracle-gated merge into `unattended`, driven end-to-end by Werft — through bootstrap mode, the review queue, and the first green `werft-oracle` check.

## 1. System overview

Five runtime pieces on one VM, one stateful service:

| Piece | What it is |
|---|---|
| **manager** | One Python process, one uvicorn worker, one event loop. Scheduler + API + static dashboard. The only thing that holds a Postgres connection. |
| **postgres** | The queue (`FOR UPDATE SKIP LOCKED`), the event bus (`LISTEN/NOTIFY`), the metrics store (SQL views), the only state. |
| **runner** (per run) | An ephemeral capable dev-box container: root inside, package installs, services, headless browser. Created cold per run, destroyed after; mutations die with it. |
| **egress-proxy** | Squid; per-run allowlists including the project's package registries. Runners have no other way out. |
| **GitHub** | The oracle (Actions, hosted runners), the backlog (issues), the merge machinery (PRs, branch protection). Werft polls; zero inbound listeners. |

Supporting containers: `dns-guard` (runner DNS, rate-limited/logged) and `docker-socket-proxy` (manager's Docker API path) — both kept from [A§10.1]; cheap, and each closes a real channel.

There is no Redis, no Celery, no broker, no metrics stack, no Kubernetes.

## 2. Technology stack (pinned 2026-07-31)

From [BP§15]'s primary-source re-verification over [A§2]:

| Layer | Pin |
|---|---|
| Host | Rocky Linux 10, SELinux **enforcing**; x86-64-v3 vCPU asserted at install |
| Containers | Docker CE **≥ 29.6.2** (29.4.2 forbidden; floor tracked as "current maintained major", re-checked dated); bundled containerd version verified at install |
| Host kernel | Floor asserted in `install.sh` (CVE-2026-31431/KEV); `{"selinux-enabled": true}` in `daemon.json` (arms Docker's seccomp/LSM mitigation) |
| Database | PostgreSQL 18.x latest minor, digest-pinned, volume at `/var/lib/postgresql` (never the pre-18 `/data` path) |
| Manager | Python **3.14**; uv + committed hash-verified `uv.lock`; FastAPI **0.140.x**; **`starlette>=1.3.1` pinned explicitly**; uvicorn 0.51.x, single worker; Pydantic **≥ 2.13** + pydantic-settings; SQLAlchemy 2.0.51+ async + asyncpg (pin exact at first `uv lock`) + Alembic 1.18.x; structlog 26.1.x; import-linter 2.13 + ruff, both exact-pinned (CI gates) |
| HTTP client | httpx 0.28.1 exact; successor is **httpx2** (import-rename migration) — decide at first manager scaffold, don't drift into it |
| Dashboard | Svelte 5 + Vite + Tailwind, exact-pinned at scaffold time; static build served by the manager; Node 24 LTS |
| CI / oracle | GitHub Actions, `ubuntu-24.04` (never preview runners under the oracle); `actions/checkout` v7; **classic branch protection, not rulesets** (auto-merge is broken under ruleset-sourced required checks); repository-level Actions budgets set to stop-usage, not alert-only; zizmor (`zizmorcore/zizmor`) on Werft's own workflows |
| Backup / alerts | pg_dump + restic (offsite); ntfy |

Security floors before first boot [BP§15.1]: the Docker/kernel/SELinux floors above; a written **`docker cp` prohibition** against live runners; dependency-cooldown config (npm's `min-release-age` per [BP§15.1 C5] — reconcile the exact key name against the npm docs at image-build time; uv equivalent) baked into runner images — under capable boxes this cooldown is the only supply-chain damper on agent-initiated installs, so it ships in the base image, not as advice.

## 3. Domain: projects and runs

### 3.1 Projects

A `projects` row per managed repo. New in this spec: `lifecycle ∈ {bootstrap, oracle_gated}`.

- **bootstrap**: the project has no proven CI. Run PRs wait for the operator in the review queue. The standing goal of bootstrap runs is to produce the project's harness and `werft-oracle` workflow.
- **oracle_gated**: the `werft-oracle` check exists and has been seen green. Run PRs wait on executed CI; green auto-merges.
- **Flip**: automatic on the first green `werft-oracle` check observed on a run PR head. The flip applies the strict branch protection ([§6.3]) in the same action, writes a `project_events` row, and notifies the operator. Manual flip (both directions) exists via API for repair.

### 3.2 Run state machine

Eleven states. Terminal: `merged`, `canceled` only. `parked` is never terminal — it always admits human requeue.

`queued, claimed, running, awaiting_ci, awaiting_review, merging, blocked_quota, failed, parked, merged, canceled`

| From | To | When |
|---|---|---|
| queued | claimed | dispatch: claim CAS + quota reservation in one transaction |
| queued | blocked_quota | provider exhausted / ceiling reached at dispatch |
| queued | parked | PermanentError pre-attempt (bad config, repo 404) |
| claimed | queued | lease expired before container start |
| claimed | running | container started |
| claimed | failed | hard-deadline sweep |
| running | awaiting_ci | attempt ended with PR, project oracle_gated |
| running | awaiting_review | attempt ended with PR, project bootstrap |
| running | failed | attempt failure (incl. vanished container via lease) |
| awaiting_ci | queued | CI red, retry budget left → fresh dispatch |
| awaiting_ci | merging | CI green on up-to-date head |
| awaiting_ci | failed | unrecoverable infra error while waiting (never a timeout) |
| awaiting_ci | parked | CI red with budget spent, or `ci_timeout` |
| awaiting_review | merging | operator accepts |
| awaiting_review | parked | operator rejects (`parked_reason='review_rejected'`) |
| awaiting_review | failed | PR/repo gone out-of-band (infra only — a review may wait indefinitely, no timeout) |
| merging | merged | squash-merge landed |
| merging | awaiting_ci | base moved (oracle_gated); rebased result must re-earn green |
| merging | parked | merge conflict / merge blocked |
| merging | failed | unrecoverable infra error |
| blocked_quota | queued | wake at `exhausted_until` / window headroom — automatic, no human |
| failed | queued | retry with backoff (`next_attempt_at`) |
| failed | blocked_quota | provider exhausted |
| failed | parked | retry budget spent, or PermanentError |
| parked | queued | human requeue |
| *any non-terminal* | canceled | human cancel |

Enforcement [A§4.2/§4.4]: the transition table lives in a DB table enforced by a `BEFORE UPDATE` trigger **and** as a pure table in `domain/`, with a contract test asserting they are identical. Every transition is a CAS (`... WHERE id=:id AND version=:v`); trigger + `run_events` insert + `pg_notify` in one transaction.

Attempt outcomes are typed from day one and recorded in `run_attempts`: `quota_exhausted` is distinct from `ci_red` and `agent_failure`, and never consumes retry budget — budget exhausted must mean N genuine failures, not N interruptions [CLP§3.5.3]. Dispatch behavior is named: this spec ships only `retry` (force-reset branch to base); `continuation` (from branch HEAD) is a later second kind, not a rework.

### 3.3 Concurrency invariants (kept verbatim from [A§4/§5/§7])

1. One writer: only the manager holds a Postgres connection. Runners are DB-blind — file in (`task.json`), files out (`result.json`, `log.jsonl`), exit code.
2. `runs` is the queue, the state, and the row. Claim via `FOR UPDATE SKIP LOCKED` on a partial index.
3. Claim + quota reservation in one transaction — no reservation, no claim; `pg_advisory_xact_lock` per account; one candidate per transaction.
4. Short-lived `advance()` handlers; long waits live in DB columns (`next_attempt_at`, `lease_expires_at`), never in sleeping coroutines.
5. NOTIFY is latency; the 15 s reconciliation tick is correctness. No outbox — side effects are derived from run state, idempotent via unique indexes and state-guarded updates.
6. At most one live run per backlog item (partial unique index).
7. A second uvicorn worker is forbidden by design — it would be a second scheduler.

## 4. Runner: the capable dev box

### 4.1 Image

One base image per project, **built on the VM by Werft** from a Werft-owned, human-authored Dockerfile (resolves [BP§14] defect #1 — no registry delivery path needed, no CI-built images). Digest-recorded on each run. Contains the project's toolchain, the provider CLI, git, a headless Chromium, and dependency-cooldown config. **No agent-authored bytes ever enter an image build** [CD M4]; agents mutate the running container, never the image. BuildKit default `docker` driver only; `docker-container`/rootless builders and kaniko rejected [BP§15.5].

### 4.2 Hardening posture (capable-box BASE)

The create-body is built from three components [CD M1/M3/M6]: a `BASE` dict that never varies; an enumerated **per-run computed component** (container name, `NetworkMode` = the run's network, `Dns` = that network's dns-guard address, mount source paths); and a closed, typed per-project delta — permit **columns, not rows**. The body test asserts byte-equality on BASE, key-enumeration on the per-run component (no other key may ever appear there), schema on the delta, and explicit negative assertions.

```text
ReadonlyRootfs=false; User absent (root inside — the capable-box decision)
CapDrop=["ALL"]; CapAdd=<minimum empirically tested set> (expected: CHOWN, FOWNER,
  DAC_OVERRIDE, SETUID, SETGID, KILL; never SYS_ADMIN/SYS_PTRACE/SYS_MODULE/NET_ADMIN/NET_RAW)
SecurityOpt=["no-new-privileges:true"]; default seccomp; SELinux labeling on (:Z workspace)
Privileged=false; no host {Pid,Ipc,Network,Cgroup}Mode; no Devices; no VolumesFrom;
  no Docker socket; no published ports; no ExtraHosts
Per-run internal network with egress-proxy + dns-guard as the only way out
  (NetworkMode/Dns live in the per-run computed component, key-enumerated)
PidsLimit=4096; ShmSize=1g (Chromium); Memory/NanoCpus capped per project config;
  tmpfs /tmp; disk bounded by manager-side polling with hard kill
Mounts: workspace rw :Z; outputs dir rw :z (manager-readable, see §4.3);
  task.json ro; secrets/ ro directory; nothing else
```

The exact `CapAdd` floor is settled by [CD§2.4(ii)]'s one-afternoon empirical test during implementation (root + CapDrop=ALL, add caps only on observed failure) and then locked by the create-body test. Chromium runs `--no-sandbox` (its sandbox needs userns the default seccomp blocks; consistent with the pragmatic posture). `userns-remap` stays rejected (operator decision 2026-07-25, reaffirmed 2026-07-31 as part of accepted residual risk).

### 4.3 Contract and completion

- In: `task.json` (issue snapshot, repo, branch, config). Workspace: the manager clones the repo into the run workspace **before container start** (manager-side; removes the clone-failure class from the adapter and any mirror machinery — resolves [BP§14] defect #7).
- Out: `result.json` (atomic write; the behavioral completion contract — `status` drives control flow; only free-text and token counts are display-only, resolving [BP§14] defect #3), `log.jsonl` (transcript stream), artifacts ([§8]).
- Completion signal: container `die` event (Docker events stream + reconciliation inspect; never blocking `wait`, never log content) + inspected exit code + `result.json`. Exit codes: `0` contract fulfilled (result.json written, valid); `2` CLI unstartable; `4` workspace/git failure; `5` result serialization failure; anything else = adapter crash.
- `result.json.status` set includes `quota_exhausted` from day one.
- **Enforcement is manager-side.** A root agent can kill or patch the in-container adapter, so the run ceiling (90 min default), tree-kill, and teardown are enforced by the manager via the Docker API; in-container redaction is best-effort only — the real mitigation is credential scoping ([§4.4]). SELinux labeling is decided once, here, as [CD M8] demands: the **workspace** is `:Z` (private to the runner); the run's **outputs directory** (`result.json`, `log.jsonl`, artifacts) is a separate mount with the shared container label (`:z`) so the containerized manager can read it after exit. The shared label is outputs-only, and cross-run reach still requires a mount that never exists — runner A has no path to runner B's outputs. An integration test locks manager read-back (resolves [BP§14] defect #4). Host-side readers never traverse non-regular files: `lstat`, `followlinks=False`, running-total cap — never `cp -r`/`rsync -a`/`tar -C` [CD M8].

### 4.4 Credentials in the box

- **GitHub:** per-run installation token from the Werft GitHub App, attenuated to `contents: write` on the one repo, delivered via `GIT_ASKPASS` file in the ro secrets directory, re-minted by rename before expiry, **revoked at teardown** [CD S2/S3]. Redaction handles both `ghs_` token formats (36-char and `ghs_APPID_JWT` long form) by exact value, not regex shape.
- **Provider:** the Claude Code credential (`claude setup-token` output) is manager-held and mounted ro. **Accepted exception, stated plainly:** this is one whole-account credential shared by every Claude runner — "scoped short-lived credentials" is true of the GitHub token only. A run can burn the account's quota or get it flagged; blast radius is the operator's personal account (the ToS risk from [A§13#9] is accepted and unchanged — personal accounts are the point of the product).
- Nothing else enters the box. No npm/PyPI publish credentials, no DB access, no Werft API token.

### 4.5 Egress

Per-run squid ACLs: the project's declared registry set (e.g. `registry.npmjs.org`; PyPI needs both `pypi.org` **and** `files.pythonhosted.org`; a dnf mirror set), provider API hosts, and the project's declared extra hosts. RFC1918/private destinations denied ahead of the allowlist. Registry access is an **accepted exfiltration channel**: with registries reachable, a compromised run can exfiltrate repo contents via request shapes; the mitigation is scope-of-secrets ([§4.4]) and per-run egress logs collected as evidence ([§8]) — not channel closure. The rewriting pull-through mirror stays rejected with its trigger acknowledged as fired [CD§8]. Per-project registry lists live in Werft config (not in the project repo — agents could edit the repo).

## 5. Provider: Claude Code adapter

- Invocation: `claude -p` with `--output-format stream-json` piped to `log.jsonl`, final envelope parsed for `result.json`. Never `--bare` (mutually exclusive with the OAuth token and discards `CLAUDE.md` — [BP§15.3]); Werft context injected via `--append-system-prompt-file`.
- Model: a config value per project/work-type; this spec hardcodes no model IDs (resolves [BP§14] defect #8).
- Outcome classification: parse the CLI's own envelope — `rate_limit` → `quota_exhausted` (+ reset time when present), `refusal` → `policy_block`, auth errors → `auth_failure` (alert: re-auth needed). Account-level failures emit plain stderr with no JSON envelope — classified by stderr match, never filed as parse errors, or the re-auth/quota alerts never fire. `error_max_budget_usd` maps to `agent_failure` (it is a CLI-side budget, not provider quota).
- **Usage-reader duty** (doctrine #4): (a) in-band limit warnings/blocks → `exhausted_until` + reset time — reliable, load-bearing; (b) per-run token/cost fields from the JSON envelope → ledger input; (c) `/usage`-class utilization data **if** it proves script-readable — de-risked by an early implementation spike, explicitly not load-bearing for the milestone. Driving the authenticated claude.ai usage page stays rejected for quota (ToS posture unchanged).

## 6. GitHub integration

### 6.1 Topology (unchanged doctrine)

`main` (promotion PRs only; required checks + 1 human review, include-admins) ← `unattended` (run + sync-back PRs only; strict required `werft-oracle` check once oracle-gated, include-admins) ← `werft/run-<id>` (ephemeral, off `unattended` HEAD, force-reset per attempt, deleted on merge/terminal).

### 6.2 Mechanics

Polling only: issues 60 s, PR/check status 30 s, ETag conditional requests. PR-open is idempotent (adopt-on-422). Oracle convention: one non-matrix job named `werft-oracle` in `.github/workflows/werft-oracle.yml`. Merge is `strict_serialized` per project. For **oracle_gated** projects: update-branch, wait for green on the updated head (= the merged result), squash-merge; base-moved re-enters `awaiting_ci`. For **bootstrap** projects there is no check to wait for: operator accept executes update-branch + immediate squash-merge, retrying the update+merge once if GitHub rejects on a base move (merges are serialized per project, so that happens only on an external push); a conflict parks as `merge_blocked` — an operator acceptance is never silently converted into a CI wait that cannot go green. Terminal-path PRs are closed and branches deleted (no CI-burning zombies). Mid-run backlog edits never kill an in-flight run; unlabel/close marks the item ineligible for future dispatch.

### 6.3 Bootstrap protection ordering (resolves the greenfield chicken-and-egg)

At onboarding of a bootstrap project: create `unattended` from `main`, apply **partial protection** (no force-push, no deletion — no required checks yet, since the workflow does not exist). Run PRs park in the review queue; the operator's accept merges them. At the flip ([§3.1]) the manager applies the strict required-check protection. The GitHub App permission ceiling: contents rw, PRs rw, issues rw, checks read, actions read, administration read, metadata read [A§8.6].

Deferred but specified in lineage, unchanged: promotion ceremony, sync-back with its non-strict check, CODEOWNERS oracle self-protection, `touches_tests` surfacing — all activate with promotion, after the milestone.

## 7. Quota: the minimal self-cap

One dimension for the milestone: **metered provider-CLI wall-clock seconds over the provider's rolling 5-hour window** (deterministic with zero provider cooperation; run-count as fallback config; tokens are never admission inputs [CLP C2]).

- Admission (inside the claim transaction): `consumed_in_window + reservation ≤ ceiling_seconds`, reservation trued-up at CLI exit. A reservation is also released — trued-up to zero or to observed container seconds — **in the same transaction as any transition out of `claimed`/`running` that does not pass through CLI exit** (lease expiry, hard-deadline sweep, cancel): no path leaks headroom. One knob for the thin loop (the `ceiling × conservative_factor` two-knob split returns with multi-provider quota).
- **Provider-reported rules, mechanically:** a parsed provider refusal sets `exhausted_until` (+ reset time) and always beats the ledger's optimism; while it is in the future nothing dispatches, and `blocked_quota` runs requeue automatically at that time (unattended means no human in this path). A readable utilization figure is recorded with source + timestamp and **tightens only** [CLP§3.2]: when convertible to window-seconds (optional config `provider_window_capacity_seconds`) admission uses the larger of ledger-consumed and provider-derived consumed; when not convertible, a reading at ≥ 95% utilization blocks dispatch outright and anything less is display-only. A provider reading never loosens the ledger in the thin loop; readings older than 15 min are ignored. "Provider numbers rule" means refusals and readings always win in the safe direction — the ledger never overrides a provider signal.
- Lowering a ceiling refuses new reservations; it never kills in-flight work.
- Display (even in the bare UI): consumed, reserved, ceiling, headroom, `exhausted_until` with source and timestamp — shown separately, no arithmetic asked of the operator.

## 8. Evidence: the minimal store

Collect-by-default (doctrine #6): after each run the manager copies `.werft-artifacts/` plus conventional paths (`playwright-report/`, `test-results/`) from the workspace to `/srv/werft/runs/<id>/artifacts/`, and persists `log.jsonl` alongside — the transcript is evidence and is retained, full, by default (privacy floor: it may contain anything the agent saw; accepted, single-operator system; the restic backup therefore contains transcripts too).

- Caps: 100 MB per run, 25 MB per file; over-cap drops largest-first, and **what was dropped is recorded as an event** — evidence that silently misses is worse than evidence that says "truncated here."
- DB: one row per artifact — `run_id, path, bytes, collected_at, content_hash NULL, event_ref NULL`. Bytes on disk, metadata in DB; the nullable `event_ref` is what lets the later timeline UI link artifacts to `run_events` without a migration.
- Browsable: an artifact listing + file-serving endpoint per run (`Content-Disposition: attachment` + `nosniff`; no `{@html}` anywhere near run-derived strings — the evidence surface is a stored-XSS surface [CD S7]).
- Evidence lives outside the repo mount and never enters commits or PRs [CLP§3.7]. Disk is a protected resource: at 90% volume usage Werft stops accepting new runs (alert), until the operator prunes — no auto-GC in the thin loop, but the release condition is named now.

## 9. Operator surface (thin-loop minimum)

- API `/api/v1`: runs list/detail, artifact listing/serving, review accept/reject, run cancel/requeue, project onboard/flip, quota status. Static bearer token, Tailscale-only, TLS via `tailscale cert`. Mutations are this closed set; a write endpoint beyond it is the signal to re-read the doctrine.
- UI: **one page** — the runs list with state, project, attempt outcomes, quota strip, and per-run links to PR + artifacts + review accept/reject buttons. The five-page dashboard returns post-milestone.
- Alerts (ntfy): review-queue item waiting, `auth_failure`, `exhausted_until` set, park, disk threshold, flip event.

## 10. Deployment

Compose services: `postgres`, `manager-migrate` (one-shot Alembic, gates start), `manager`, `egress-proxy`, `dns-guard`, `docker-socket-proxy`. Networks: `internal` (pg↔mgr), `mgr_egress`, per-run runner networks, `ingress` (Tailscale-bound port), squid's NAT leg. Zero public listeners; firewalld drops non-tailnet. Secrets are file mounts, never env. `install.sh` asserts every [§2] floor. Backup: nightly pg_dump + restic offsite (includes evidence tree). Runner workspaces under an XFS pquota mount.

## 11. Deferred (recorded, not designed here)

Second provider (Codex) and the provider-conformance suite; Kimi; local tier (Aider is abandoned upstream — a replacement harness is chosen when the tier is built); Grok/Gemini; `routing.yaml` and multi-provider chains; per-project memory store; scheduler/recurring work; non-code work types (the review queue is their landing zone); agent-proposed issues (mechanism: agents open ordinary GitHub issues, the operator's `werft:ready` label remains the sole dispatch trigger — no schema needed now); continuity beyond typed `quota_exhausted` outcomes (git-checkpoint pause/resume, `--resume`); evidence timeline UI; promotion/sync-back activation; multi-dimension quota; the full five-page dashboard.

## 12. Accepted risks (write-downs, not surprises)

1. **Container escape**: pragmatic hardening; a runc/kernel escape reaches the VM. Watched (CVE watch is an operational duty), not engineered away.
2. **Shared provider credential**: every Claude runner holds the account credential ([§4.4]).
3. **Registry egress is an exfil channel** ([§4.5]).
4. **Transcripts retained by default**, including offsite in backups ([§8]).
5. **Provider ToS / personal-account suspension**: unchanged from [A§13#9]; personal accounts are the point.
6. **The operator is a SPOF** for review-queue latency in bootstrap mode — mitigated by alerts, accepted otherwise.

## 13. The one-sentence test

Does this keep "what lands" decided exclusively by executed checks or explicit operator acceptance, in one engine, with one source of truth, operable by one person — while the agent's box stays capable and disposable?
