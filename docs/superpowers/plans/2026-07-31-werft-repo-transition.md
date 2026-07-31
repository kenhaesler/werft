# Werft Repo Transition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute the clean-slate repo transition from the approved identity-realignment design: preserve history, archive the superseded documents to `docs/lineage/`, write the new README.md and SPEC.md, and replace the 14 stale GitHub issues with the thin-loop issue set.

**Architecture:** This plan produces documents and issue state only — no application code. The thin-loop build itself gets its own plan(s) afterward, derived from the SPEC.md this plan creates. Source of truth: `docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md` (the 13 operator decisions).

**Tech Stack:** git, GitHub CLI (`gh`), Markdown. Repo: `github.com/kenhaesler/werft`, working copy `C:\Users\kenha\Documents\git\werft` (Windows; run git/gh from the repo root; shell examples are POSIX-compatible and work in Git Bash).

## Global Constraints

- The design doc governs. Where this plan and `docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md` conflict, the design doc wins and this plan has a bug.
- Never delete a lineage document — everything superseded is moved, not removed.
- `git mv` for all moves (preserve history).
- One commit per task, message format `docs(transition): <what>`, ending with the line `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- No pushes until Task 7 (single push of the finished transition), except Task 1 which pushes the wip branch and main to make the history durable before anything moves.
- GitHub operations (`gh`) only in Tasks 8–9, after the transition is merged, so issue links point at files that exist on `main`.
- File contents given in this plan are complete and final — copy them verbatim; do not improvise additions.

---

### Task 1: Preserve history — commit the uncommitted material and fast-forward main

**Files:**
- Commit (already modified/untracked, no edits needed): `README.md`, `BUILD-PLAN.md`, `docs/product-discovery/` (3 files)

**Interfaces:**
- Produces: `main` at the same commit as `wip/architecture-v1.3-currency-audit`, containing every historical document; both branches pushed. Later tasks branch from this `main`.

- [ ] **Step 1: Verify the expected starting state**

Run: `git status --short && git branch --show-current`
Expected: branch `wip/architecture-v1.3-currency-audit`; exactly ` M README.md`, `?? BUILD-PLAN.md`, `?? docs/product-discovery/` (a `?? docs/superpowers/plans/` entry for this plan file may also appear — stage it in Step 2). If anything else is modified, stop and report.

- [ ] **Step 2: Commit the pre-realignment record on the wip branch**

```bash
git add README.md BUILD-PLAN.md docs/product-discovery/ docs/superpowers/
git commit -m "docs: preserve build plan, product discovery, and README status (pre-realignment record)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 3: Fast-forward main onto the wip branch**

```bash
git checkout main
git merge --ff-only wip/architecture-v1.3-currency-audit
```
Expected: `Fast-forward` (main had no unique commits). If `--ff-only` refuses, stop and report — do not force.

- [ ] **Step 4: Push both branches**

```bash
git push origin wip/architecture-v1.3-currency-audit main
```

- [ ] **Step 5: Verify**

Run: `git log --oneline -3 main`
Expected: top commit is the Step 2 commit; `docs: identity realignment design` directly beneath it.

---

### Task 2: Create the transition branch and archive the superseded documents

**Files:**
- Move: `ARCHITECTURE.md` → `docs/lineage/ARCHITECTURE-v1.4.md`
- Move: `BUILD-PLAN.md` → `docs/lineage/BUILD-PLAN-v1.0.md`
- Move: `docs/product-discovery/core-loop-proof-2026-07-26.md` → `docs/lineage/product-discovery/core-loop-proof-2026-07-26.md`
- Move: `docs/product-discovery/containment-design-2026-07-27.md` → `docs/lineage/product-discovery/containment-design-2026-07-27.md`
- Move: `docs/product-discovery/agentic-os-gap-analysis-2026-07-27.md` → `docs/lineage/product-discovery/agentic-os-gap-analysis-2026-07-27.md`
- Move: `tests/architecture_spec.test.mjs` → `docs/lineage/architecture_spec.test.mjs`
- Create: `docs/lineage/README.md`

**Interfaces:**
- Produces: branch `transition/clean-slate`; repo root containing only `README.md`, `.gitignore`, `docs/`; `docs/lineage/` holding the complete superseded record. Tasks 3–6 run on this branch.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b transition/clean-slate main
```

- [ ] **Step 2: Move the documents**

```bash
git mv ARCHITECTURE.md docs/lineage/ARCHITECTURE-v1.4.md
git mv BUILD-PLAN.md docs/lineage/BUILD-PLAN-v1.0.md
git mv docs/product-discovery docs/lineage/product-discovery
git mv tests/architecture_spec.test.mjs docs/lineage/architecture_spec.test.mjs
rmdir tests
```
(`rmdir tests` only if now empty; if it fails because the directory is gone already, that is fine.)

- [ ] **Step 3: Write the lineage index**

Create `docs/lineage/README.md` with exactly this content:

```markdown
# Lineage — the historical record

Everything in this directory is **superseded and frozen**. It is kept because the analysis inside it (adversarial reviews, red-team findings, technology currency research) remains a reference mine during implementation. Nothing here governs the current system — that is `/SPEC.md` and `/README.md`, under the design decisions in `docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md`.

| Artifact | What it was |
|---|---|
| `v1-verdict.md` | Post-mortem of Werft v1 (Claude Agent Station): why LLM-judged verification failed. Still the founding lesson. |
| `architecture-verification.md` | Second independent verification of the v1.2 architecture ("buildable with fixes"). |
| `architecture-2026-currency-audit.md` | The 2026/2027 currency-and-completeness audit that produced architecture v1.3. |
| `ARCHITECTURE-v1.4.md` | The full groundwork specification, v1.4 — 119 KB, four adversarial passes. Superseded by SPEC.md; carries 10 verified defects catalogued in BUILD-PLAN-v1.0.md §14. |
| `BUILD-PLAN-v1.0.md` | Ten-phase build plan derived from v1.4. Its §15 technology-currency research fed SPEC.md's stack pins. Superseded. |
| `product-discovery/core-loop-proof-2026-07-26.md` | Question-at-a-time discovery log with the operator's verbatim answers; quota/evidence/continuity design. |
| `product-discovery/containment-design-2026-07-27.md` | 39-agent containment red-team of invariant I-1. Its no-root recommendation was overturned by the 2026-07-31 realignment (capable dev boxes); the data-flow controls survive in SPEC.md. |
| `product-discovery/agentic-os-gap-analysis-2026-07-27.md` | Gap analysis: "agentic OS" vs the dispatcher the spec described. Drove the realignment interview. |
| `architecture_spec.test.mjs` | The 28-test regex suite that structurally locked ARCHITECTURE.md v1.4 prose. Frozen with its subject; its file paths are intentionally broken here and it is not meant to run. |
```

- [ ] **Step 4: Verify the tree**

Run: `git status --short && ls`
Expected: renames (`R`) for all six moves plus the new `docs/lineage/README.md`; repo root shows only `README.md`, `docs`, and dotfiles.

- [ ] **Step 5: Commit**

```bash
git add docs/lineage/README.md
git commit -m "docs(transition): archive superseded architecture, build plan, discovery docs, and spec tests to lineage

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Write the new README.md

**Files:**
- Modify: `README.md` (full replacement)

**Interfaces:**
- Produces: the governing doctrine document. SPEC.md (Task 4) declares this README as the tie-breaker; issue bodies (Task 9) link to it.

- [ ] **Step 1: Replace README.md with exactly this content**

```markdown
# Werft

**A self-hosted agentic operating system for one operator's software projects.**

Werft (German: *shipyard*). You hand it a VM; the VM belongs to Werft. Agents build in capable, disposable containers; proven work merges; everything leaves an evidence trail.

Werft is the successor to Claude Agent Station (v1, archived 2026-06-05) — rebuilt around the one lesson v1 died for, and realigned 2026-07-31 around what the operator actually wants ([the design record](docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md)).

## What it does

Werft runs coding agents unattended across projects, routes each kind of work to the model you chose on the subscriptions you already pay for, protects your quota using the providers' own reported limits, and merges nothing unproven: **code merges only on green CI executed against the merged result; everything else lands only when you accept it — until a work type earns automation.** Every run leaves a first-class evidence trail: what was done, what it cost, what the agent saw.

The dev factory is the spine; the OS-feel — capable environments, per-project memory, scheduled work, non-code work — is the body it grows.

## Doctrine

These six decisions are load-bearing. Every design choice traces back to one of them.

1. **Verification is executed for code, human-gated for everything else.** The merge gate for code is green CI in a clean environment against the merged result. No LLM verdict ever merges code — v1 died here. Non-code work and bootstrap-phase projects pass through the operator's review queue, with a per-work-type path to automated acceptance once proven.
2. **Blast radius is contained by branch topology plus disposable containers.** Agents work on branches off a long-lived `unattended` branch; green-CI merges land there, never on `main`. Promotion to `main` is a human-triggered batch PR. The container is the wall: capable dev boxes (root inside, installs, services, browser) with pragmatic hardening, scoped short-lived credentials, and per-run egress rules. Residual container-escape risk is accepted and written down, not engineered away.
3. **Providers are subscription CLIs on the operator's personal accounts,** dispatched at the process layer. Claude Code first; Codex, Kimi, and a local OpenAI-compatible tier next; Grok and Gemini later. No gateway, no per-token billing in the core path.
4. **Quota truth is provider-reported.** Each adapter reads its provider's own usage and limit signals, and those numbers rule dispatch admission; Werft's own metering ledger fills gaps and estimates between readings. Self-capping below the operator's ceiling is the #1 feature.
5. **The backlog is human-approved.** Agents may propose issues; nothing is dispatched without the operator's label. v1's failure — self-dispatched work flooding verification capacity — stays structurally impossible.
6. **Evidence is a product surface.** Runs collect artifacts by default — transcripts, diffs, screenshots, browser traces — into a per-run record with size caps.

## How a run works

1. You (or, later, an approved agent proposal) label a GitHub issue `werft:ready` — the only intake path.
2. The manager claims the run and reserves quota in one transaction, prepares a workspace, and starts one ephemeral capable container with the chosen provider CLI.
3. The agent works — installs what it needs, runs what it builds — commits, and pushes. The adapter reports completion by exit code and `result.json`; artifacts are collected.
4. The manager opens a PR onto `unattended`. **Oracle-gated** projects wait for the executed CI check (`werft-oracle`) on the merged result: green auto-merges, red retries fresh while budget lasts, then parks. **Bootstrap** projects (no CI yet — their early runs exist to build it) wait in your review queue instead; first green CI flips the project to oracle-gated.
5. You promote: a batch PR `unattended → main`, CI re-runs, you merge.

## Status

**Clean slate as of 2026-07-31.** The prior groundwork (architecture v1.4, ten-phase build plan, discovery record) is archived in [`docs/lineage/`](docs/lineage/README.md). The current buildable specification is [`SPEC.md`](SPEC.md), scoped to the thin loop: the greenfield Elastic log-analysis project goes from empty repository to its first oracle-gated merge, driven end-to-end by Werft. No implementation yet — the thin-loop issues on this repo are the build.

## Anti-goals

- No second execution engine; Postgres is the queue, the event bus, and the metrics store.
- No LLM-judgment gates anywhere in the code-merge path.
- No agent access to Werft's own substrate: Werft is never an onboarded project of itself, and only Werft controls the VM.
- No state outside the database (plus the evidence files it indexes).
- The dashboard serves the loop; it never becomes the product.
```

- [ ] **Step 2: Verify links resolve**

Run: `ls docs/lineage/README.md docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md`
Expected: both exist. (`SPEC.md` intentionally does not exist until Task 4.)

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(transition): new README — identity and doctrine v2

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Write SPEC.md

**Files:**
- Create: `SPEC.md`

**Interfaces:**
- Produces: the buildable specification for the thin loop. The thin-loop implementation plan(s) and the Task 9 issues derive from its section numbers (cited as `SPEC §n`).

- [ ] **Step 1: Create SPEC.md with exactly this content**

````markdown
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
````

- [ ] **Step 2: Verify internal links**

Run: `ls README.md docs/lineage/README.md docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md`
Expected: all exist.

- [ ] **Step 3: Commit**

```bash
git add SPEC.md
git commit -m "docs(transition): SPEC.md v1.0 — the thin loop

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Update the .gitignore comment and design-doc paths

**Files:**
- Modify: `.gitignore` (only if it references moved paths — inspect first)
- Modify: `docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md` (path references to moved files)

**Interfaces:**
- Produces: no document in the repo references a path that no longer exists, except intentionally-frozen lineage files.

- [ ] **Step 1: Check .gitignore for stale references**

Run: `cat .gitignore`
If it references `tests/` or `ARCHITECTURE.md` or `BUILD-PLAN.md`, remove only those lines. The `/*.png` v1-lesson line stays.

- [ ] **Step 2: Update the design doc's file references**

In `docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md`, apply exactly these replacements (it is a historical record, but its "Supersedes" pointers should resolve):
- `ARCHITECTURE.md v1.4, BUILD-PLAN.md v1.0, and the three ` + "`docs/product-discovery/`" + ` documents` → `ARCHITECTURE.md v1.4, BUILD-PLAN.md v1.0, and the three product-discovery documents (all now in ` + "`docs/lineage/`" + `)`

No other edits to the design doc.

- [ ] **Step 3: Sweep for dangling references**

Run: `grep -rn --include='*.md' -e '](ARCHITECTURE.md' -e '](BUILD-PLAN.md' -e '](docs/product-discovery' -e '](tests/' . | grep -v docs/lineage | grep -v docs/superpowers/plans`
Expected: no output. (This plan file quotes the patterns and would self-match without the second filter — a hit inside `docs/superpowers/plans/` is expected and must not be "fixed".) Fix any other hit by pointing it at the `docs/lineage/` location.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs(transition): fix references to archived paths

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Transition review checkpoint

**Files:** none (verification only)

- [ ] **Step 1: Verify the final tree shape**

Run: `ls && ls docs && ls docs/lineage && ls docs/lineage/product-discovery`
Expected: root = `README.md SPEC.md docs` (+ dotfiles); `docs` = `lineage superpowers`; lineage = the 3 original files + `README.md ARCHITECTURE-v1.4.md BUILD-PLAN-v1.0.md architecture_spec.test.mjs product-discovery`; product-discovery = the 3 discovery files.

- [ ] **Step 2: Verify history preservation**

Run: `git log --oneline --follow -- docs/lineage/ARCHITECTURE-v1.4.md | tail -3`
Expected: the original `feat: system architecture v1.0` commit appears — history followed through the rename.

- [ ] **Step 3: Verify commit count and cleanliness**

Run: `git status --short && git log --oneline main..HEAD`
Expected: clean tree; 4 commits (Tasks 2–5).

---

### Task 7: Merge the transition to main

**Files:** none (git/GitHub operations)

**Interfaces:**
- Produces: `main` containing the new README, SPEC.md, and lineage tree; pushed. Tasks 8–9 depend on this.

- [ ] **Step 1: Push the branch and open the PR**

```bash
git push -u origin transition/clean-slate
gh pr create --base main --head transition/clean-slate \
  --title "Clean slate: archive groundwork to lineage, new README + SPEC.md" \
  --body "Executes the repo transition from the approved identity-realignment design (docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md, decision #12).

- Everything superseded moved (git mv) to docs/lineage/ with an index
- New README.md: identity + doctrine v2
- New SPEC.md v1.0: the thin loop (Elastic greenfield pilot, bootstrap mode, capable runners, Claude Code first, provider-reported quota, evidence collect-by-default)
- No application code in this PR

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 2: Merge (merge commit, keep per-task history) and sync local main**

```bash
gh pr merge transition/clean-slate --merge --delete-branch
git checkout main && git pull origin main
```

- [ ] **Step 3: Verify on GitHub**

Run: `gh repo view --web=false --json defaultBranchRef -q .defaultBranchRef.name && gh api repos/kenhaesler/werft/contents/SPEC.md -q .name`
Expected: `main` and `SPEC.md`.

- [ ] **Step 4: Account for the stray remote branch `amendments-v1.3`**

Origin carries a branch `amendments-v1.3` (at `f26174f`) that is not in the local clone. Decide its fate now so the clean slate leaves nothing unaccounted:

```bash
git fetch origin +refs/heads/amendments-v1.3:refs/remotes/origin/amendments-v1.3
git log --oneline main..origin/amendments-v1.3
```

If the log is empty (fully contained in main): `git push origin --delete amendments-v1.3`. If it shows commits, stop and report them to the operator — do not delete unreviewed history.

---

### Task 8: Close the 14 superseded issues

**Files:** none (GitHub operations)

**Interfaces:**
- Consumes: `main` with SPEC.md (Task 7).
- Produces: zero open issues from the old plan; each closed with the same pointer comment.

- [ ] **Step 1: Close all 14 with the pointer comment**

```bash
for n in 1 2 3 4 5 6 7 8 9 10 11 12 13 14; do
  gh issue close "$n" --comment "Closed by the 2026-07-31 clean-slate realignment. The operator re-decided the project's identity and first build (see docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md and SPEC.md on main); the phase plan this issue came from is archived in docs/lineage/BUILD-PLAN-v1.0.md. The replacement issue set is labeled thin-loop."
done
```

- [ ] **Step 2: Verify**

Run: `gh issue list --state open`
Expected: empty (until Task 9 creates the new set).

---

### Task 9: Create the thin-loop issue set

**Files:** none (GitHub operations)

**Interfaces:**
- Consumes: SPEC.md section numbers (Task 4).
- Produces: 11 new issues, all labeled `thin-loop`; the epic additionally labeled `epic`, T0 additionally labeled `decision`.

- [ ] **Step 1: Create labels**

Note: `decision` and `epic` already exist on the repo (from the old issue set) with different colors/descriptions; `--force` redefines them, which also changes how they render on the 14 closed historical issues. That is intended — one current meaning per label.

```bash
gh label create thin-loop --description "First milestone: empty Elastic repo to first oracle-gated merge" --color 1D76DB --force
gh label create decision --description "Operator decision required" --color D93F0B --force
gh label create epic --description "Milestone tracking issue" --color 5319E7 --force
```

- [ ] **Step 2: Create the epic**

```bash
gh issue create --label epic --label thin-loop --title "EPIC: Thin loop — empty Elastic repo to first oracle-gated merge" --body "The milestone from SPEC.md: the greenfield Elastic log-analysis project goes from empty repository to its first oracle-gated merge into its unattended branch, driven end-to-end by Werft — through bootstrap mode, the operator review queue, and the first green werft-oracle check.

Order of work (SPEC §-refs in each issue):
1. Werft repo scaffolding + its own CI
2. Spine: domain + DB + migrations
3. Capable runner + container lifecycle
4. Claude Code adapter + usage reader
5. GitHub integration + bootstrap flip
6. Review queue + operator API/UI minimum
7. Quota self-cap minimum
8. Evidence collection minimum
9. Deploy: compose + install floors
0. (operator) Create the Elastic project repo

Done means: a real issue on the Elastic repo traverses label → run → PR → review-accept (bootstrap) repeatedly until its CI exists, the project flips to oracle_gated on first green, and the next run merges on green CI with no human in the merge path. A deliberately broken change must go red and not merge."
```

- [ ] **Step 3: Create the work issues**

```bash
gh issue create --label thin-loop --label decision --title "T0 (operator): create the Elastic log-analysis project repo" --body "Operator actions, blocking T5 end-to-end testing:
- Decide the repo name (suggestion: elastic-log-analyst) and create it empty (private is fine) with only a short README stating the product idea: an AI-based log-analysis tool for Elasticsearch.
- Install the Werft GitHub App on it (permission ceiling per SPEC §6.3).
- Decide initial per-project config values: registry allowlist for its stack, memory/CPU caps, model choice.
The project onboards in bootstrap lifecycle (SPEC §3.1); its early Werft runs will build the harness and werft-oracle workflow."

gh issue create --label thin-loop --title "T1: Werft repo scaffolding and CI" --body "SPEC §2. manager/ Python 3.14 + uv scaffold with pinned pyproject (fastapi 0.140.x, starlette>=1.3.1 explicit, pydantic>=2.13, sqlalchemy 2.0.51+ async, asyncpg, alembic 1.18.x, structlog, httpx 0.28.1 exact — and record the httpx2 decision), ruff + import-linter exact-pinned, pytest. GitHub Actions CI for Werft itself on ubuntu-24.04 with actions/checkout v7, zizmor over workflows. Layered import contract enforced from the first commit. Acceptance: CI green on a hello-world manager package; import-linter fails the build on a deliberate cross-layer import."

gh issue create --label thin-loop --title "T2: Spine — domain, DB, migrations" --body "SPEC §3. projects (with lifecycle bootstrap|oracle_gated) + project_events + runs (11-state machine) + run_attempts (typed outcomes incl. quota_exhausted) + run_events + quota ledger + artifact metadata tables. Transition table in DB with BEFORE UPDATE trigger AND mirrored pure-Python domain table; contract test asserts identity. CAS transitions; trigger + run_events + pg_notify in one transaction. Acceptance: every illegal transition rejected by the database itself; alembic up/down/up clean; property test over the transition table."

gh issue create --label thin-loop --title "T3: Capable runner — image build, lifecycle, hardening" --body "SPEC §4. Per-project image built on the VM from Werft-owned human-authored Dockerfile (digest recorded per run). Create-body builder: BASE dict + enumerated per-run component (name, network, dns, mount paths) + closed typed per-project delta; byte-equality on BASE, key-enumeration on the per-run component, negative assertions in tests. Root inside, CapDrop=ALL + empirically-settled CapAdd floor (run the one-afternoon test: dnf/npm/uv/service/chromium under CapDrop=ALL, add only on observed failure, lock result in the test). no-new-privileges, default seccomp, :Z workspace, per-run network via egress-proxy + dns-guard, PidsLimit 4096, ShmSize 1g, manager-side disk polling + hard kill, manager-side 90-min ceiling. Manager-side clone before container start. Host-side readers: lstat, no symlink traversal, running-total cap. Acceptance: two concurrent runs cannot read each other's workspace; symlink-attack artifact tree yields zero bytes copied; ps -eZ shows container_t; no leaked networks/volumes after teardown."

gh issue create --label thin-loop --title "T4: Claude Code adapter and usage reader" --body "SPEC §5, §4.4. claude -p --output-format stream-json (never --bare), context via --append-system-prompt-file, credential from claude setup-token mounted ro. Envelope parsing to result.json; classification: rate_limit→quota_exhausted(+reset), refusal→policy_block, auth→auth_failure, error_max_budget_usd→agent_failure; account-level plain-stderr failures classified by stderr match, never as parse errors. Usage reader: in-band limit messages → exhausted_until (load-bearing); per-run token/cost → ledger; spike: is /usage-class data script-readable (not load-bearing). GitHub token via GIT_ASKPASS ro file, re-mint by rename, revoke at teardown, exact-value redaction for both ghs_ formats. Acceptance: adapter conformance fixtures for each classification; kill -9 of the CLI mid-run yields a classified failed attempt, not a hang."

gh issue create --label thin-loop --title "T5: GitHub integration — poller, PRs, oracle, bootstrap flip" --body "SPEC §6. Issue poller (werft:ready, 60s, ETag), PR open (idempotent, adopt-on-422), check-status poller (30s), strict_serialized squash-merge, terminal-path PR/branch cleanup, mid-run backlog-edit rules. Onboarding applies partial protection to unattended (no force-push, no deletion; no required checks yet); the flip (first green werft-oracle on a run PR head) applies strict protection, writes project_events, notifies. Bootstrap accept-merge: update-branch + immediate squash-merge, no check wait (SPEC §6.2). Acceptance: on a bootstrap project a run PR waits in review; on an oracle_gated project a green PR auto-merges and a deliberately red PR never merges; kill -9 of the manager at each step recovers via reconciliation."

gh issue create --label thin-loop --title "T6: Review queue and operator surface minimum" --body "SPEC §3.2, §9, §8 (XSS floor). awaiting_review state wired to accept (→merging, manager merges) / reject (→parked review_rejected, requeueable). /api/v1: runs list/detail, review accept/reject, cancel/requeue, project onboard/flip, quota status, artifact endpoints. Static bearer token, Tailscale-bound, tailscale cert TLS. UI: single runs-list page (Svelte 5, static build served by manager) with review buttons, quota strip, artifact links. ntfy alerts: review waiting, auth_failure, exhausted_until, park, disk, flip. Acceptance: full bootstrap round-trip through the UI; a script tag in an artifact filename/run field renders inert."

gh issue create --label thin-loop --title "T7: Quota self-cap minimum" --body "SPEC §7. One dimension: provider-CLI wall-clock seconds in the rolling 5h window; reservation inside the claim transaction, true-up at exit; ceiling change never kills in-flight; exhausted_until always wins; blocked_quota auto-requeues at wake time with no human; provider utilization reading (when readable) overrides ledger until 15-min staleness. Acceptance: synthetic-clock window tests; N concurrent claim racers never exceed the ceiling; a parsed limit-reached message blocks dispatch until its reset time then auto-resumes."

gh issue create --label thin-loop --title "T8: Evidence collection minimum" --body "SPEC §8. Post-run collection of .werft-artifacts/ + playwright-report/ + test-results/ into /srv/werft/runs/<id>/artifacts/ with log.jsonl retained; 100MB/run + 25MB/file caps, largest-first drop, truncation recorded as an event; artifact rows (run_id, path, bytes, collected_at, content_hash NULL, event_ref NULL); listing + serving endpoints (attachment + nosniff); 90% disk threshold stops new runs + alerts. Acceptance: cap enforcement test records exactly which files were dropped; collection of a hostile tree (symlinks, FIFOs, dir loops) completes with zero non-regular bytes."

gh issue create --label thin-loop --title "T9: Deploy — compose, install floors, backup" --body "SPEC §10, §2. docker-compose: postgres (18.x digest-pinned, /var/lib/postgresql), manager-migrate, manager, egress-proxy, dns-guard, docker-socket-proxy; networks per SPEC §10; secrets as file mounts. install.sh asserts: x86-64-v3, Docker CE >= 29.6.2 (29.4.2 refused) + bundled containerd check, kernel floor, selinux-enabled daemon flag, XFS pquota workspace mount, firewalld non-tailnet drop. Nightly pg_dump + restic including /srv/werft/runs. Acceptance: fresh-VM install from scratch reaches a healthy manager answering on the tailnet; every floor assertion fails loudly when violated."
```

- [ ] **Step 4: Verify**

Run: `gh issue list --label thin-loop`
Expected: 11 open issues (epic + T0–T9).

---

## Self-review record

- **Spec coverage:** design §5 items 1–4 map to Tasks 1–2 (commit + archive), 3 (README), 4 (SPEC), 8–9 (issues), 7 (branch/merge). Design §4 (thin loop) is deliberately NOT implemented here — it is specified in SPEC.md and ticketed in Task 9; its implementation gets its own plan.
- **Placeholders:** none — all file contents and commands are final text.
- **Consistency:** SPEC section numbers cited in Task 9 issue bodies match the SPEC.md text in Task 4 (§2 stack, §3 spine, §4 runner, §5 adapter, §6 GitHub, §7 quota, §8 evidence, §9 surface, §10 deploy).
- **Adversarial verification (2026-07-31):** four independent reviewers (design fidelity, internal consistency, command correctness against live repo/GitHub state, technical soundness) produced 16 findings — 1 major (bootstrap runs had no workable merge path: the only base-moved edge led to a CI wait that cannot go green on a check-less project) and 15 minors (quota-reservation leak on abnormal exits, tighten-only restatement of the provider-utilization override, BASE-dict vs per-run-network contradiction, SELinux outputs-dir `:z` mechanism decided per CD M8, README fidelity wording, issue-body §-refs and table names, label redefinition note, sweep self-match filter, stray `amendments-v1.3` remote branch step). All 16 fixed in this text.
