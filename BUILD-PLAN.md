# Werft — Build Plan

**Status:** v1.0 (2026-07-27). The ordered, phased path from an empty repository to a working
prototype. Derived from [ARCHITECTURE.md](ARCHITECTURE.md) v1.4 plus the three product-discovery
documents, with the operator decisions of 2026-07-27 applied.

**Governance.** [README.md](README.md) doctrine governs. `ARCHITECTURE.md` is the blueprint. This
document is the **order of work** — it adds no design, only sequence, deliverables and acceptance
criteria. Where it appears to contradict the architecture, the architecture wins and this document
has a bug.

**Reading order for someone starting the build:**

1. `README.md` — the five doctrine points. Non-negotiable.
2. `ARCHITECTURE.md` — the blueprint. §14's one-sentence test governs every change.
3. `docs/product-discovery/core-loop-proof-2026-07-26.md` — what the first release is.
4. `docs/product-discovery/containment-design-2026-07-27.md` — what must be true of the runner.
5. This file — what to build, in what order, and how you know a phase is done.

---

## 0. The rules this plan follows

| # | Rule | Why |
|---|---|---|
| R1 | **Every phase has an executed acceptance test.** Not a checklist, not a review — a command that passes or fails. | Doctrine #1 applied to Werft's own construction. A phase "done" by inspection is the v1 failure mode. |
| R2 | **No phase depends on a later phase.** | A phase you can't finish is a phase you shouldn't have started. |
| R3 | **The oracle exists before anything is built against it.** | `docs/lineage/v1-verdict.md`: *"Start from the oracle. Build the verifier first."* |
| R4 | **Every architecture change lands in `ARCHITECTURE.md` in the same PR as its code**, with a matching lock in `tests/architecture_spec.test.mjs`. | §11 change discipline. Silent drift is how v1 grew three engines. |
| R5 | **Deferred work goes in §12's reject ledger with a written revisit trigger** — never into a backlog. | The ledger is the mechanism that keeps scope closed. |
| R6 | **Containment invariant I-1 is checked at every phase that touches the runner.** | The container is now the only wall (containment design §3). |

---

## 1. Phase map

```
P0  Preconditions ─────────────► nothing built; everything unblocked
     │
P1  Spine (domain · db · contracts) ──────────► pure logic, zero I/O, exhaustively tested
     │
P2  Environment contract + per-project image ──► the agent has a place to work
     │
P3  Runner plane (container · adapter · hardening) ──► an agent can execute, contained
     │
P4  Orchestrator + GitHub ──────► ★ THE LOOP: first merged run. Doctrine #1 and #2 proven.
     │
P5  Quota self-cap ─────────────► the operator's #1 feature, no provider telemetry needed
     │
P6  Surface (API · dashboard · alerts) ──► operable by one person
     │
P7  Second provider ────────────► the provider contract is a contract
     │
P8  Continuity (pause · checkpoint · continuation) ──► interrupted work survives
     │
P9  Evidence + model routing ───► transparency and per-work-type model selection
     │
    Deferred (§12 ledger, with triggers)
```

**P4 is the milestone that matters.** Everything before it is scaffolding; everything after it is
enhancement. If the project stops at P4 it has still proven the thing v1 could not.

---

## 2. Phase 0 — Preconditions

**Builds no Werft code.** Three independent tracks; run them in parallel.

### P0.1 — Make the pilot a valid oracle (blocking, highest risk)

Verified 2026-07-27: `pcapng-inspector` has **no `.github/workflows/` directory**, no Python
lockfile (every dependency a floating `>=` range), a `pyshark` dependency requiring the external
`tshark` binary, and AI code paths under test. It has 32 backend test files — a real suite, and the
reason it is a good pilot once hardened.

| # | Deliverable | Acceptance |
|---|---|---|
| P0.1.1 | `.github/workflows/werft-oracle.yml` — **one non-matrix job named `werft-oracle`** (§8.2 convention) | Workflow runs green on a clean PR |
| P0.1.2 | Every `uses:` pinned to a 40-hex commit SHA; `actions/checkout` v7+ without `allow-unsafe-pr-checkout`; read-only `GITHUB_TOKEN`; plain `pull_request` trigger | `zizmor` passes with zero findings |
| P0.1.3 | Backend dependency lock (`uv.lock`, hash-verified); `tshark` pinned to an exact version in the workflow | `uv sync --frozen` succeeds; suite runs |
| P0.1.4 | Suite is hermetic — no live model calls, no network. Stub every `openai` code path under test | Suite passes with network disabled |
| P0.1.5 | Frontend build + lint participate in the same single check | One check name covers both halves |
| P0.1.6 | Coverage-floor or test-count-delta step (§8.2 (c)) | A PR that deletes a test turns the check red |
| P0.1.7 | **Ten consecutive green runs on a clean runner** | 10/10. Anything less means the oracle is flaky, and a flaky oracle is indistinguishable from agent failure |
| P0.1.8 | Oracle-strength attestation recorded (§8.6 step 3): does it build, run the real suite, lint, and would it catch a plausibly-wrong change? | Written, dated, signed by the operator |

> **P0.1.7 is the real gate.** Every measurement in the proof — provider pass rates, retry counts,
> attempt budgets — is meaningless against a flaky oracle.

### P0.2 — Host preparation

Follows §10.8 steps 1–3. Deliverable: a Rocky Linux 10 VM with Docker CE, SELinux enforcing and
**asserted in the daemon**, Tailscale, firewalld, the `werft` user, `/opt/werft/{compose,config,secrets,backups}`,
and the three systemd timers.

**Acceptance —** `install.sh` runs idempotently and asserts, failing loudly on any:

- `x86-64-v3` vCPU
- **Docker Engine ≥ 29.6.2, and *not* 29.4.2** (§15.1 C1 — the current `≥ 26.x` floor admits an
  engine unpatched against three full container-breakout CVEs, a `docker cp` host-root exec, and a
  firewalld-reload port re-exposure that lands squarely on this host's posture)
- **A kernel floor covering CVE-2026-31431 "Copy Fail"** (§15.1 C2 — CVSS 7.8, **on CISA KEV,
  actively exploited**, AF_ALG container escape). The spec has no kernel assertion at all today
- `docker info` reports SELinux under SecurityOptions — **now doubly load-bearing**: this is the
  switch that arms Docker's Copy Fail LSM mitigation on a Rocky host, not merely `:Z` hygiene
- `dns-guard` is the runners' only resolver
- `/srv/werft` mounted `nosuid,nodev`
- overlay2 on XFS with `pquota` (per-run storage limits)

Express the Docker floor as a **policy, not a fixed integer** — Docker publishes no LTS and no EOL
calendar, so a hardcoded number goes stale on a ~12-month clock. Assert *"on the current Maintained
major per moby `BRANCHES-AND-TAGS.md`"*, with 29.6.2 as today's concrete value.

### P0.3 — Decisions

> **The `unshare -Ur` experiment is already answered — do not run it.** Verified 2026-07-27 from
> moby's shipped `profiles/seccomp/default.json`: `unshare` appears exactly once in the file, gated
> on `CAP_SYS_ADMIN`, under a `SCMP_ACT_ERRNO` default. An unprivileged user namespace **cannot** be
> created in a runner under the current hardening. Rootless in-container installs (bubblewrap, nix,
> rootless buildah) are out; the no-root recommendation has no cheaper alternative beside it.
> See §15.2.

| Item | Owner | Blocks |
|---|---|---|
| **D-a** — accept the no-root trade? (language installs yes, system installs via manifest + rebuild) | Operator | P2, P3 |
| **D-b** — amend I-1, or make §6.6's credential order binding? | Operator | P3, P7 |
| **D-c** — environment manifest home: Werft-side, or repo-side request + Werft-side truth? | Operator | P2 |
| **D-d** — resource ranges now, or hold at 2 vCPU / 4 GB until the scheduler question? | Operator | P3 |
| **D-e** — ship the SHOULD control list, or defer into §12 with triggers? | Operator | P3 |

**Phase 0 acceptance:** pilot oracle 10/10 green and attested; `install.sh` asserts clean on the
real VM; all six decisions recorded in the architecture.

---

## 3. Phase 1 — The spine

**Pure logic and persistence. Zero network, zero Docker, zero GitHub.** This is the half of the
system that v1 got right and is cheapest to get right again.

| Module | Deliverable |
|---|---|
| `domain/` | The 10 states, the complete `TRANSITIONS` table, the error taxonomy (`TransientError`, `ProviderError`, `QuotaExhaustedError`, `GitConflictError`, `PermanentError`), one `RetryPolicy` map. **Imports nothing.** |
| `db/` | Alembic migrations for every table in §4.3, the `BEFORE UPDATE` transition trigger, the `AFTER INSERT` created trigger, every partial index, `pg_notify` on one channel |
| `contracts/` | Pydantic models for `task.json` and `result.json`, shared verbatim by manager and adapter |
| `config/` | `Settings` (pydantic-settings) + `RoutingTable` (YAML), one `load_app_config()` in the composition root |

**Dependency floors to set in `pyproject.toml` before the first `uv lock`** (§15): an explicit
**`starlette>=1.3.1`** — it is transitive under FastAPI, pinned nowhere, and FastAPI's own
`>=0.46.0` floor will not carry the fix for CVE-2026-54283 (CVSS 7.5) or the four other 2026
advisories. Bump FastAPI to 0.140.x and Pydantic's floor to 2.13. State exact versions for uvicorn,
Alembic, structlog, import-linter and ruff rather than "current" — an unpinned CI gate can fail on a
day nothing in the repo changed, which is the same reproducibility argument §2 already makes for
runner toolchains.

**Schema deltas from v1.4 to apply here** (so later phases don't migrate twice):

- `runs.parked_reason` CHECK gains `'user_paused'`, `'stale_paused'`, `'needs_environment'`, `'env_failure'`
- `runs.slot SMALLINT` + partial unique index `WHERE status IN ('claimed','running')` — mirrors `ux_runs_one_active_per_item`, allocated inside the claim CAS (containment M7)
- `runs.runner_image_digest TEXT` — written in the claim CAS beside `routing_rules_hash`; without it, per-project images silently confound the outcome record doctrine #4 depends on
- `runs.environment_id` → `project_environments(id)`
- `run_attempts.outcome` gains `'env_failure'`
- `project_environments` table: `{id, project_id, env_digest, manifest JSONB, image_ref, status ∈ (declared|building|ready|failed|superseded), granted_by, granted_at}`
- `projects.oracle_attested_by/at`, `projects.active_environment_id`
- `result.json.status` gains `paused`; exit tier `6 = environment/provision failure`

**Acceptance (all executed):**

1. Every legal transition succeeds; **every illegal transition is rejected by the database**, not just by Python.
2. Contract test: DB `run_status_transitions` ≡ `domain.TRANSITIONS`, exact set equality.
3. `werft.contracts` models round-trip the adapter's serialization byte-for-byte.
4. Integration (testcontainers, real Postgres): concurrent claim attempts produce exactly one winner; CAS no-ops on a lost race; the trigger fires `run_events` + `pg_notify` in the same transaction.
5. `alembic upgrade head` then `downgrade base` then `upgrade head` is clean.
6. import-linter: `domain` imports nothing; the middle five modules do not import each other.

---

## 4. Phase 2 — Environment contract and per-project images

**This phase is what makes the agent able to do real work, and it is where I-1 is won or lost.**

### The design (containment design §7)

The environment declaration is a **manifest, not a Dockerfile**, and it lives in **Werft-owned
state** at `/opt/werft/config/environments/<slug>.yaml`, bind-mounted read-only into the manager
exactly like `routing.yaml`.

Allowlisted keys only — distro package names, toolchain versions, ecosystem lockfile paths,
environment variable **names** (never values), writable-path list, resource request, artifact globs,
test-path globs. **No `FROM`. No `RUN`. No `COPY`. No build args. No container-create flags.**
Pydantic with `extra='forbid'`.

> **The load-bearing property:** approval can never mean *"approve arbitrary root code"*, only
> *"approve a package name."* One schema decision closes the agent-authored-`RUN` case, the
> hostile-`FROM` case, and the baked-credential case, with zero runtime machinery.

**The project may request, never declare.** A repo may carry `.werft/env-request.yaml`, read **for
display only, never as a build input** — the same firewall §6.4 applies to logs. Read it from
`unattended` HEAD post-merge, never from a run branch, or an agent can manufacture operator
attention on demand.

### Bake the dependencies, not just the system packages

The image is built from the project's **committed lockfiles**, so `.venv` and `node_modules` are
present before the container starts.

**Consequence, and it is the reason this phase comes before the loop:** the agent can run the
project's tests immediately, with **zero runtime installs and zero registry egress**. Slice 1 needs
no registry mirror at all. Runtime installs become the exception (a genuinely new dependency), not
the norm — and that exception parks with `parked_reason='needs_environment'` rather than reaching
for the network.

| # | Deliverable | Notes |
|---|---|---|
| P2.1 | Manifest Pydantic model in `werft.contracts`, `extra='forbid'` | Rejects `privileged`, `runArgs`, `mounts`, `capAdd`, `securityOpt`, `network` at schema validation — the guard against a "just use devcontainer.json" implementation |
| P2.2 | Compiler: manifest → Dockerfile against a pinned `FROM werft-runner-base@sha256:…` | Werft owns the template; the project owns only data |
| P2.3 | `make build-env <slug>` — human-run, host-side, **`BUILD=0` never moves** | The build capability is not on the manager's side of the socket proxy at all |
| P2.4 | Digest recorded in `project_environments.image_ref`; **dispatcher creates containers by digest only, never by tag** | Closes tag hijack |
| P2.5 | `gitleaks` over the rendered Dockerfile in Werft's own CI | §10.2 already establishes gitleaks as the house control |
| P2.6 | §6.6 gains one sentence: *"No credential may enter an image layer; if a build ever needs one, BuildKit `--mount=type=secret` only."* | §6.6 defines exactly two trust models, both runtime mounts; an image layer is an unruled third |
| P2.7 | `$HOME` placement rule: toolchains, language runtimes and caches install **outside `$HOME`** (`/opt/werft-env/…`), with env vars baked in | §6.3 mounts a tmpfs over `$HOME`, which would otherwise **silently shadow everything the image baked there** — the agent sees "command not found" and starts reinstalling |
| P2.8 | Resolve §6.2's three-way contradiction (hand-built / CI-built / no registry) and name the delivery path | CI-built images currently have no stated route to the VM's daemon cache |

**Acceptance:**

1. The pilot's manifest builds an image containing a pinned `tshark`, the frozen backend venv, and `node_modules` from `package-lock.json`.
2. A container from that image runs the pilot's **full test suite offline**, with `runner_net` attached and no registry host allowlisted — and the result matches CI.
3. A manifest containing `privileged`, `capAdd`, `runArgs`, `mounts`, `securityOpt` or `network` is **rejected at schema validation**.
4. `which uv && which node && echo $VIRTUAL_ENV` all resolve inside a running container (proves P2.7).
5. `gitleaks` finds nothing in the rendered Dockerfile.
6. Test-locked: `EXEC=0 VOLUMES=0 BUILD=0 SYSTEM=0` asserted in `tests/architecture_spec.test.mjs` — **verified 2026-07-27: the suite currently contains no such assertion.**

---

## 5. Phase 3 — The runner plane

**Where invariant I-1 is enforced.** Everything here is manager code, because §10.1 states plainly
that the socket proxy *"does not inspect request bodies"* — manager code is the only enforcement
point that exists.

### P3.1 — The create-body builder (the highest-value component in the system)

```
BASE (never varies, never grantable, asserted by equality AND explicit negatives):
  CapDrop         = ["ALL"]
  CapAdd          = []                              # empty, permanently
  SecurityOpt     = ["no-new-privileges:true"]      # label=disable never present
  ReadonlyRootfs  = true                            # writable PATHS, not a writable /
  User            = "1000<slot>:1000<slot>"         # per-run uid (M7)
  NetworkMode     = "werft-run-<slot>"              # per-run net, STATIC per-slot subnet (M6)
  Dns             = [<dns-guard @ static slot IP>]  # static, so BASE really is invariant
  PortBindings    = {}
  Privileged      = false
  Devices, DeviceCgroupRules, VolumesFrom, Runtime,
  CgroupParent, Sysctls, ExtraHosts,
  {Pid,Ipc,Userns,Cgroup}Mode                       = ABSENT (not false — absent)
  Labels          = {"werft.run_id": <id>}

WRITABLE SURFACE (paths, not a filesystem):
  bind  runs/<id>/workspace  → /work            rw, :Z
  bind  runs/<id>/task.json  → /task.json       ro     # separate mount
  bind  runs/<id>/secrets    → /run/secrets     ro     # DIRECTORY — so re-mint by rename works
  bind  runs/<id>/prefix     → <install prefix> rw     # NOT /usr/local, NOT a named volume
  tmpfs /tmp                 size=1g, nosuid, nodev
  tmpfs $HOME                size=512m, nosuid, nodev  # now SIZED

CLOSED TYPED DELTA (the only per-project variance; ranges, not free values):
  Image      digest form only, "…@sha256:…"; tags rejected
  NanoCpus / Memory / PidsLimit / Mounts ⊆ generated writable-path set
```

Three corrections carried in from the adversarial audit, each of which the obvious implementation
gets wrong:

- **Not a named volume for the install prefix.** Named volumes are auto-created on container create,
  but removal needs `DELETE /volumes/{name}`, which `VOLUMES=0` blocks — one orphaned volume per
  run, unremovable through the sanctioned control plane. Use a bind mount from the run directory.
- **Not `/usr/local` as the writable prefix.** The adapter is **PID 1** and enforces the wall-clock
  ceiling, the tree-kill and the token redaction. A writable `/usr/local` hands the agent the
  adapter's own `site-packages` mid-run.
- **Static per-slot subnets.** Dynamic per-run subnets would break §6.7's *static* squid config and
  falsify the "BASE never varies" claim, because `Dns` would change per run.

**Acceptance — five assertions, replacing "the dict is present":**

1. **Equality:** `create_body == BASE | delta`, `delta.keys() ⊆ CLOSED_DELTA_KEYS`, every value in range. **Any unknown key fails.**
2. **Explicit negatives on the final body:** `"Privileged" not in body`, `CapAdd == []`, no `*Mode == "host"`, no `SecurityOpt` containing `unconfined` or `label=disable`. *(Equality alone passes trivially if someone adds `Privileged: true` to `BASE` — this is what catches that.)*
3. **Property test over the entire grant space:** for every representable manifest, (1) and (2) still hold.
4. **Mount-source containment:** every bind source, after `realpath`, resolves under this run's own directory or the named read-only per-project cache.
5. **Source purity:** an import-linter contract that `runner/` cannot import anything that reads the workspace.

### P3.2 — Container lifecycle

Event-driven completion per §6.1: one supervised Docker events reader filtered to
`label=werft.run_id`; a `die` event enqueues the run; the handler reads the exit code via
`docker inspect`, reads `result.json` off the bind mount, then explicitly removes the container. Per
run: `docker network create` → create → start → (die) → inspect → read → `rm` → `network rm`.

### P3.3 — Adapter runtime

PID 1; `setsid` + tree-kill (`killpg` TERM → 10 s → KILL) + blocking `os.wait()` reap to `ECHILD`;
JSONL log with token redaction; idle-output watchdog; atomic `result.json` write.

**Raise the idle-output watchdog default.** A long silent build or install phase produces no CLI
stdout and would be killed at 15 minutes — recorded as a **provider timeout**, poisoning the routing
evidence doctrine #4 depends on for a provider that did nothing wrong. Suspend it during the
declared provision phase and emit structured `tool_call_started/ended` lines so log growth stays a
true liveness signal.

**Relabel the adapter's limits as hygiene, not containment** (§6.5/§9.1 wording). The `du` cap, the
idle watchdog, the wall-clock ceiling and token redaction all live inside the agent's own box. They
are hygiene against sloppiness, not controls against hostility — if anyone later leans on them as
containment, the design is wrong.

### P3.4 — Host-side reader discipline

`os.walk(followlinks=False)` + `lstat`; copy only regular files and directories; cap enforced as a
**running total during** the walk; modes stripped; artifact path validated once at onboarding.
Explicitly prohibited in the text: **not `cp -r`, not `rsync -a`, not `tar -C`** — the shortcut a
solo operator reaches for under pressure.

Also resolve here: **§9.1 and §6.3 disagree on the scope of the `:Z` label.** §6.3 labels
`workspace/`; §9.1 says *the run directory* is `:Z`. Read literally, §9.1 means the manager cannot
read `result.json` or `log.jsonl` and **the entire completion path fails.** Decide it deliberately;
do not "fix" it by downgrading to `:z`.

**Phase 3 acceptance:**

1. The five create-body assertions pass, including the property test.
2. A container runs the pilot suite and the manager reads exit code + `result.json` correctly.
3. `ps -eZ` shows the runner process as `container_t` (§11 item 4).
4. **Two concurrent runners:** run A gets `EACCES` on `/srv/werft/runs/<B>/workspace/`, and cannot reach B over the network.
5. **Symlink test:** a workspace containing `ln -s /opt/werft/secrets/github-app.pem artifacts/report.html` produces **zero bytes copied** and one truncation event.
6. `kill -9` of the manager mid-run: reconciliation resumes the run on restart.
7. `docker network ls` is clean after a run — no leaked networks, no leaked volumes.

---

## 6. Phase 4 — ★ The loop

**The milestone.** One project, one provider (Codex), one model: issue labeled `werft:ready` → run →
PR → green oracle → auto-merge to `unattended` → human promote → sync-back.

| Module | Deliverable |
|---|---|
| `orchestrator/` | The scheduler: LISTEN reader, 15 s reconciliation tick, `advance()` handlers (short-lived, always), CAS transitions, retry policy at exactly one decision point |
| `github/` | App auth, installation-token minting **and revocation on teardown**, ETag poller (issues 60 s, checks 30 s, divergence 5 min), PR create with 422-adopt, `update-branch`, squash-merge, sync-back on a **non-strict** context |
| `routing/` | `resolve_chain` + `decide_dispatch`, pure, zero I/O |
| `cli/` | `werftctl onboard`, `drain`/`undrain`, `doctor`, `env build|approve` — talks **only** to the API |

**Two small high-value items that are easy to miss:**

- **Revoke the installation token on teardown** and at the 45-minute re-mint (one
  `DELETE /installation/token` in the handler that already removes the container). The per-run token
  currently outlives its container by up to an hour. Highest value-per-line in the whole design.
- **Attenuate the per-run token to `contents: write`.** GitHub's mint endpoint accepts a permissions
  subset, and the manager opens PRs and comments itself — the runner never needs those scopes.

**Acceptance — the proof itself:**

1. Label a real issue on the pilot. A run traverses `queued → claimed → running → awaiting_ci → merging → merged` with no manual intervention.
2. A **deliberately broken** change goes red in CI and **does not merge**. It retries, then parks. Doctrine #1, executed.
3. Nothing ever lands on `main` without the human Promote click. Doctrine #2, executed.
4. Promote opens the batch PR **frozen to `promotions.from_sha`**; the manifest Ken reads equals what lands.
5. Sync-back `main → unattended` lands on the non-strict `werft-syncback` check.
6. `kill -9` the manager at each of five points in the lifecycle; each time reconciliation resumes correctly.
7. A run's diff contains **no Werft artifacts** — no `AGENTS.md`, no `CLAUDE.md`, no run-directory paths, no installed dependency tree.

> **Stop here and evaluate before building further.** P4 answers the only question that matters:
> does the loop work on real code with a real oracle? Phases 5–9 are worth building only if it does.

---

## 7. Phase 5 — Quota self-cap

The operator's stated #1 feature. **It needs no provider telemetry** — which is why it ships this
early.

```
Werft may start work on account A iff:

  (1) for every declared dimension d ∈ {window_runs, window_wallclock_s, weekly_runs}:

        werft_consumed[d] + reservation[d]
            ≤ plan_capacity[d] × utilization_ceiling × conservative_factor

  (2) and the provider has not refused:
        exhausted_until(A) IS NULL  OR  now() ≥ exhausted_until(A)
```

`utilization_ceiling` **is** the reserve ("leave me 40 %" = `0.60`). `conservative_factor` **is** the
safety margin (default `0.80`; the operator's 5–10 % preference maps to `0.95`–`0.90`). They are
different things — intent versus measurement error — which is why they multiply rather than stacking
as a third knob.

**Exactly one pessimism factor, up to three intent ceilings.** Any future proposal to add a fourth
multiplier must state the resulting worst-case product in the same breath.

**One correction the decision forces:** container wall-clock stops being a proxy for provider
consumption the moment the container does substantial non-provider work. Provisioning, `uv sync` and
builds would otherwise land in the same number `"limit Claude to 60 %"` is denominated in.
**Meter CLI-start → CLI-exit** (the adapter is already PID 1 and already writes timestamps) and
record provisioning wall-clock separately as display-only. An attempt that dies before the CLI
starts releases its reservation rather than truing up to container lifetime.

**Acceptance:**

1. Synthetic-clock tests for rolling-window arithmetic across window edges, DST, and week boundaries.
2. Concurrency test: N workers racing one account never exceed the cap (the advisory lock + guarded insert holds under `READ COMMITTED`).
3. `('provider','claude',0.60)` set at runtime causes the next dispatch past 60 % to **fall through the chain**, not to fail.
4. A run killed before CLI start leaves **zero** wall-clock in the ledger.
5. Lowering a ceiling below current usage refuses new reservations and **kills nothing in flight**.

---

## 8. Phase 6 — Operator surface

`api/` (~20 endpoints, six dashboard mutations, a separate enumerated ops row), `observe/`
(`run_events`, single SSE channel with `?since_id=` resume, alerts, 15 metrics as SQL views), and the
five-page dashboard.

**Quota page must show, separately and without arithmetic:** plan limit · Werft consumption ·
configured ceiling · safety factor · outstanding reservations · resulting admissible headroom ·
provider `exhausted_until` with source and timestamp.

**Two safety items at this surface:**

- **No `{@html}` for any run-derived string**, and artifacts served with `Content-Disposition:
  attachment` + `nosniff`. The dashboard's write surface includes **Promote** — the single human
  gate — on an origin whose auth is one static bearer token. Stored XSS there is a bigger finding
  than the symlink at the same code site.
- **Alerts carry only manager-owned values** — `parked_reason` (a CHECK-constrained enum), run id,
  project, link. Agent text stays on the run detail page inside a visually-marked block.

**Acceptance:** promote flow works end to end; SSE reconnect with `?since_id=` loses no events;
`/healthz` reflects a stopped scheduler; A1–A8 each fire once under synthetic conditions and respect
cooldown; a run whose `result.json` contains a script tag renders inert.

---

## 9. Phase 7 — Second provider

Claude Code against the **same** provider contract, with no adapter-shaped special cases.

Model IDs verified 2026-07-27: `claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`,
`claude-haiku-4-5`. **ARCHITECTURE.md §4.3's `'claude:claude-opus-4-8'` example is
previous-generation and should be refreshed.**

> **§6.5's Claude adapter cannot work as written — fix it here.** Three verified findings (§15.3):
> `--bare` **does not read `CLAUDE_CODE_OAUTH_TOKEN`** and skips OAuth/keychain entirely, so it is
> mutually exclusive with *both* credential paths §6.6 depends on — the adapter either fails auth or
> silently falls to API-rate billing, breaking doctrine #3's flat-rate premise. `--bare` also
> discards the dispatcher-written `CLAUDE.md` shim, so the agent would run with **zero Werft
> context**; pass it via `--append-system-prompt-file` instead. And account-level failures (expired
> session, suspension, hit cap) emit **plain stderr, not the JSON envelope** — so the unconditional
> parse-failure rule files all three as generic parse errors and **A6/A8 never fire**, which is
> exactly the blindness those alerts exist to prevent.

Adds the T1 telemetry tier — signals the provider hands back **during sanctioned work** (the CLI's
own JSON envelope: rate-limit stop reason, `resets_at`). Writes `provider_accounts.exhausted_until`.
Stays display-only for anything the scheduler doesn't need: store the reported value **with source
and timestamp**; the boolean "the provider refused" is authoritative for *whether* to fall through,
the manager's ledger-edge arithmetic decides *how long*.

Plus the opaque-provider acknowledgement flow (§3.4 of the discovery spec): warn, accept once, store
with the configuration, invalidate when the configuration changes. An acknowledged opaque provider
is **still subject to the self-cap** — the acknowledgement removes the guarantee, not the cap.

**Acceptance:** a **provider conformance suite** both adapters pass identically — outcome
classification for success, CI-red, quota-exhausted, auth-failure, policy-block and parse-failure;
`quota_exhausted` falls through the chain **without consuming the retry budget**; any JSON parse
failure maps unconditionally to `error/outcome_parse_failed` with **no** best-effort text scan.

---

## 10. Phase 8 — Continuity

Three separable things, of which the discovery spec originally conflated all three and picked the
most fragile as primary:

| | Mechanism | Status |
|---|---|---|
| Run continuity | `blocked_quota` → wake → re-dispatch | Already in the architecture |
| **Work preservation** | **The git commit is the checkpoint** | The actual gap |
| Provider-session resume | `claude --resume <id>` | Optional; never load-bearing |

**The checkpoint is a git commit.** On a graceful-pause signal or a mid-run quota rejection, the
adapter stops the CLI, commits WIP to `werft/run-<id>` with a marker trailer, pushes, and writes
`result.json{status: "paused"}`. Content-addressed, durable on GitHub *and* in the host mirror,
inspectable with ordinary `git`, survives `kill -9` of manager and runner alike, and lives in git
and the DB — the two places the architecture already declares as state.

**Dispatch splits in two.** *Retry* (after CI-red or agent failure) force-resets to `unattended`
HEAD, as today — attempt N+1 must start clean. *Continuation* (after a pause) starts from
`werft/run-<id>` HEAD with a compact handoff. **A continuation must not consume the retry budget**,
or "budget exhausted" stops meaning "N genuine failed attempts".

**Who resumes:** quota pause resumes **automatically** at the computed wake time — that is what a
quota-aware scheduler *is*, and with a 5-hour window and `max_concurrent_sessions = 1`, requiring a
click per pause would end unattended operation. Every other pause class is explicit.

**Paused runs expire** (`WERFT_PAUSED_MAX_AGE`, default 14 days) → `parked_reason='stale_paused'`.
Without this, "resume is user-controlled" plus "protected from cleanup" means a run nobody resumes
holds its workspace forever on a single 500 GB disk. Every protected resource needs a release
condition.

**Provider-session resume, if built at all:** the session id is an **opaque hint** in the DB, resume
is attempted at most once, **any** failure falls through silently to the git checkpoint, and no
correctness property depends on it. If those conditions ever look inconvenient, drop the
optimization — do not relax them.

**Acceptance:** interrupt a run mid-work; the commit lands on the branch; the continuation starts
from it and completes; the retry budget is unchanged; a corrupt checkpoint parks rather than looping;
a run paused past the expiry parks as `stale_paused` and its evidence returns to normal GC.

---

## 11. Phase 9 — Evidence and model routing

**Evidence: Werft collects artifacts; it never drives a browser.** A project declares an artifact
directory; the manager copies it out under `WERFT_ARTIFACT_CAP` (default 100 MB) using P3.4's
link-safe walker; artifacts are linked from the timeline against the event that produced them.

*The reason matters more than the rule:* evidence is a by-product of **executed checks** (doctrine
#1). A Werft-driven browser would be an unexecuted, Werft-authored observation. Note that the
per-project image moves Playwright from "impossible" to "an image-size and artifact-cap question" —
so the surviving reason must be the doctrinal one, not the practical ones.

**Retention is deny-by-default.** Prompts and responses are not captured unless a project opts in.
General-purpose redaction of secrets from arbitrary agent output and screenshots is not a solvable
problem at this scale, and pretending otherwise would be a false assurance.

**Model routing** closes a real gap in `ARCHITECTURE.md`: `routing.yaml` selects a provider chain and
**has no model key anywhere**, while model-per-work-type is the operator's primary routing
requirement. Chain entries become `{provider, model}` pairs (a bare string keeps meaning "that
provider's default model"); `task.json` gains `model`; models validate against a per-provider
allowlist so a retired model name fails loud at reload rather than at dispatch.

`quota_ledger.model` and `usage_limits(scope='model', …)` **already exist** — the per-model ceiling
is currently unreachable from routing, which is itself evidence the key is missing.

**Not in this phase:** multi-phase runs ("Fable 5 for plans, then Opus 5 for the coding"). That is a
different execution model — several provider invocations with handoffs inside one work item — and
the architecture defines one issue = one run = one container = one invocation. §12, with a trigger.

---

## 12. Deferred — §12 ledger entries with triggers

| Item | Revisit trigger |
|---|---|
| Multi-phase runs | A measured quality gain that a single-model run cannot reach |
| Provider-session resume as a *primary* mechanism | Never — it is permitted only as an optimization nothing depends on |
| Registry pull-through mirror | **Fires the moment any package registry appears on the §6.7 allowlist** |
| `userns-remap` | **The operator grants container-root to any project** (not merely a runc CVE) |
| gVisor / Kata / microVM | A runc-escape CVE with no cheaper mitigation — now the *only* external trigger covering the "container is the only wall" residual, so pair it with the CVE watch |
| Self-hosted CI runners | Never on capability grounds. §8.2's *"self-hosting that execution on the same VM as the manager and its database is a lateral-movement gift"* is the same argument that governs build-input purity |
| docker-in-docker / nested runtime | Never via a capability grant — under I-1 a nested runtime **is** control over the container runtime |
| Agent-triggered or run-branch-sourced image rebuild | **Never.** Parking is the answer |
| Forecasting, anomaly detection, curated evidence export | After the ledger has real data |

---

## 13. Cross-cutting, every phase

1. **Architecture parity.** Any change to doctrine-adjacent behaviour updates `ARCHITECTURE.md` in the same PR, with a lock in `tests/architecture_spec.test.mjs`. **Note `tests/architecture_spec.test.mjs:30` pins `v1.4 (2026-07-25)` — the suite fails on the first architecture edit.** Handle it deliberately, in the same PR, as the version lock it is.
2. **Werft's own CI** gates every PR: ruff, import-linter, the create-body property test, unit + integration suites.
3. **One end-to-end rehearsal per release** on the real VM (§11 item 4), including `kill -9` mid-run, the `container_t` assertion, and the two-runner isolation check.
4. **Werft is never an onboarded project of itself. Ever.**
5. **CVE watch** appended to Appendix A's existing monthly base-image item — the "container is the only wall" residual has a revisit trigger in §12 that nothing currently monitors.

---

## 14. Bugs in ARCHITECTURE.md v1.4 to fix while building

Found by adversarial review on 2026-07-27, each verified against the shipped text. Fix each in the
phase that touches it.

| # | Defect | Fix in |
|---|---|---|
| 1 | §6.2 asserts three incompatible things — hand-built only / built by Werft's CI / no registry. CI-built images have **no delivery path** to the VM's daemon cache | P2 |
| 2 | §2's containers row still justifies its reject with *"the VM is the declared blast-radius boundary"* — the premise I-1 withdraws — in the most-read summary table | P3 |
| 3 | §4.3 calls `result` *display-only* and `error_message` *never branched on*, while §6.4 has `result.json.status` drive chain fallthrough | P1 |
| 4 | §9.1 vs §6.3 on `:Z` scope. Read literally, §9.1 means the manager cannot read `result.json` and **the whole completion path fails** | P3 |
| 5 | §6.5 runs claude with `--bare` *to skip `CLAUDE.md` auto-discovery* while writing a `CLAUDE.md` that imports `AGENTS.md`. Both are asserted; one must go | P7 |
| 6 | §10.1 puts `local-inference` on `runner_net` (`internal: true`) only, while §6.6 option 2 — the *strongest* credential posture — requires it to reach a provider API. **Not buildable on the stated topology** | P7 |
| 7 | §6.3 vs §6.4 on the mirror: exit code `3 = clone failure` sits in the *runner's* tier, implying the mirror is mounted, but §6.3's mount table never lists it | P3 |
| 8 | §4.3's `'claude:claude-opus-4-8'` model example is previous-generation (current: `claude-opus-5`) | P7 |
| 9 | `PidsLimit 256` was sized for one CLI plus git and does not survive a development environment; the failure is invisible to every classification mechanism Werft has | P3 |
| 10 | Appendix A promises a `routing.yaml` resource override that §7.1's schema never defines | P2 |

---

## 15. Technology currency — verified 2026-07-27

Re-verified against **primary sources** (vendor advisories, release notes, EOL calendars, shipped
source files) across six dimensions, each change-verdict independently adversarially judged. Two of
the assessors' verdicts were **overturned** by the judge and are recorded as such.

**A pin changes only when there is a concrete reason.** On a solo-operator system a version bump
that buys nothing is a cost, not a win. Most of the stack is healthy — the changes below are the
exceptions.

### 15.1 — SECURITY: change before first boot

| # | Change | Evidence |
|---|---|---|
| **C1** | **Docker CE floor `≥ 26.x` → `≥ 29.6.2`**, and **explicitly forbid 29.4.2** | 26.x is **unmaintained and out of scope for moby security advisories** (moby `project/BRANCHES-AND-TAGS.md`); only 29.x and 25.0-security (ends 2026-12-04) are maintained. A host at the current floor is unpatched against: the **runc breakout trio** CVE-2025-31133 / -52565 / -52881 (fixed 28.5.2); **CVE-2026-41567** `docker cp` resolving archive binaries via in-container `PATH` **as host root** (29.5.1); **CVE-2026-34040** AuthZ bypass CVSS 8.8 (29.3.1); and **CVE-2025-54388** — *a firewalld reload drops Docker's rules and published ports become reachable from the local network*, which lands directly on §10.1/§10.3's firewalld posture (28.3.3). 29.4.2 is a trap: it broke 32-bit and left a bypassable socketcall path |
| **C2** | **Add a kernel floor assertion to `install.sh`** — the spec has none | **CVE-2026-31431 "Copy Fail"** — Linux `crypto/algif_aead.c` AF_ALG container escape, CVSS 7.8, **on CISA KEV (actively exploited, public exploit)**. The page cache is shared across containers and the host, so it escapes a container on an unpatched kernel. Docker mitigates via seccomp/LSM from **29.4.3**, and that mitigation **only arms when SELinux support is enabled in the daemon** — which makes §10.1's existing `{"selinux-enabled": true}` assertion doubly load-bearing, not merely `:Z`-label hygiene |
| **C3** | **Pin `starlette>=1.3.1`** explicitly in §2 and the manager's `pyproject` | Starlette is **transitive under FastAPI and pinned nowhere**; FastAPI's own floor is `>=0.46.0` and will not carry the fix. Five 2026 advisories, of which the highest is **CVE-2026-54283 (CVSS 7.5, patched only in 1.3.1)**: `request.form()` silently ignores `max_fields`/`max_part_size`. *(The assessor proposed `>=1.0.1`; the judge found it closed only one of five and corrected the floor upward.)* |
| **C4** | **Write down the `docker cp` prohibition** as an invariant, not a convention | CVE-2026-41567/41568/42306 need three preconditions. **Werft satisfies two on every single run** — a volume mount always exists, and the runner is by construction a hostile process able to swap symlinks at the mount destination. Only "nobody runs `docker cp` against a live runner" protects it, and that is currently unwritten |
| **C5** | **Adopt dependency cooldown** — `min-release-age` (npm) and the uv equivalent | Three lines of config in the runner image, **zero new services**. Does **not** reopen §12's registry-proxy reject, which rejected cooldown *infrastructure*. It covers the exact gap C6 exposes |
| **C6** | **Correct the lockfile claim's scope** in §6.5 | Partially refuted: per Socket's SANDWORM_MODE analysis, *"the worm runs on package import regardless of installation method — lockfiles provide no protection since malicious code executes during the `require()` phase."* Keep lockfile-only installs; restate what they buy (pinning against fresh publishes) and what they do not (import-time execution) |

### 15.2 — The `unshare -Ur` experiment is answered: NO

**P0.3's experiment does not need to be run.** Answered from moby's shipped
`profiles/seccomp/default.json`, not from memory: `defaultAction` is `SCMP_ACT_ERRNO`, and
`unshare` appears **exactly once in the entire file**, inside a block gated on `CAP_SYS_ADMIN`.

Under `CapDrop=ALL` + `no-new-privileges` + the default seccomp profile, an unprivileged user
namespace **cannot** be created inside a runner. Consequences:

- **Rootless in-container installs are not viable** — bubblewrap, rootless buildah/podman, nix's
  sandbox and user-namespace overlays are all out. This closes a whole branch of the containment
  design rather than leaving it to be re-tested.
- **It strengthens the no-root recommendation** (containment design §2), which no longer has a
  cheaper alternative sitting beside it.
- **Per-container user namespaces are Podman-only.** Docker's `--userns` accepts exactly one value:
  `host`. Remapping is daemon-wide or nothing. The containment design's hedged *"understanding is
  the latter"* can be stated as verified fact — and §12's `userns-remap` reject gains a second
  independent reason: enabling it **pins the host to the deprecated graph-driver backend forever**,
  because the containerd image store (Docker 29's default for fresh installs) is unavailable under
  `userns-remap`.

### 15.3 — Provider adapters: `--bare` is spec-breaking

Three findings that together mean **the Claude adapter as written in §6.5 cannot work**:

1. **`--bare` is mutually exclusive with both credential paths the spec relies on.** Anthropic's
   auth docs, verbatim: *"Bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN`. If your script passes
   `--bare`, authenticate with `ANTHROPIC_API_KEY` or an `apiKeyHelper` instead."* That rules out
   §6.6's whole-account session **and** `claude setup-token`, which §6.6 lists as a *preferred*
   scoped credential. As specified the adapter either fails auth or silently falls through to
   API-rate billing — breaking the flat-rate premise doctrine #3 rests on.
2. **`--bare` also discards the `CLAUDE.md` shim** the dispatcher writes — so the Claude adapter
   would run with **zero Werft context**, defeated by the very flag mandated in the same paragraph.
   Keep writing `AGENTS.md` (Codex and Aider read it natively); pass context to Claude via
   `--append-system-prompt-file`.
3. **Account-level failures emit plain stderr, not JSON.** An expired session, a suspended account
   and a hit weekly cap all produce no JSON envelope — so §6.5's unconditional *"JSON parse failure
   → `error/outcome_parse_failed`"* files every one of them as a generic parse error and **alerts
   A6 and A8 never fire.** That is precisely the invisible-failure mode those alerts exist for.

Also: **`--json-schema` does not validate the envelope** — it constrains the *model's* output into a
separate `structured_output` field. Reword the parenthetical. And map the envelope precisely:
`subtype`/`stop_reason` of `rate_limit` → `quota_exhausted`; `refusal` → `policy_block`;
`error_max_budget_usd` → a signal the spec currently has no mapping for.

**`requiredMinimumVersion` (§13 #2) is mis-described** — it is a managed-settings policy key *Werft
itself would set*, documented to **fail open**, not something a vendor pushes at consumer accounts.
The genuine version-refusal risk is server-side model retirement.

**Aider is effectively abandoned:** last feature release 2025-08-09 (~11.5 months), PyPI description
still advertising Claude 3.7 Sonnet, no security-response commitment. It sits **inside the runner
executing agent-authored code**. This is the same governance pattern §13 #13 already flags for
httpx, but §2 presents Aider as an unqualified current choice. Give it the same treatment.

**httpx's successor is `httpx2` (Pydantic), not aiohttp.** The spec's claim is confirmed — last
stable 0.28.1 (2024-12-06), issue creation restricted — but the migration is an **import rename**,
leaving the ~200-line Docker Engine wrapper and every call site untouched, versus aiohttp's full
API rewrite. Demote aiohttp to second fallback.

### 15.4 — Verified correct; no change

Rocky 10.2 (active support to 2030-05-31) · PostgreSQL 18.4 (EOL 2030-11-14; PG19 still beta as of
2026-07-16 — do not chase it) · **Python 3.14** (full bugfix support to **2027-10-05** — the
strongest row in §2; the runway argument holds exactly as written) · Node 24 (Active LTS until
2026-10-20, EOL 2028-04-30 — 2027 runway intact) · SQLAlchemy 2.0.51 with the 2.1/greenlet warning
still accurate · uvicorn, Alembic, Pydantic, pydantic-settings, structlog, import-linter, ruff, uv ·
Actions pricing ($0.006/min, 2,000/3,000 included) · **Merge Queue still unavailable on personal
accounts** · `actions/checkout` v7 · zizmor (now `zizmorcore/zizmor`) · the GitHub App permission
ceiling, including that **omitting `workflows` is a genuine server-enforced second lock** on the
oracle, independent of CODEOWNERS · `ubuntu-24.04` (do **not** adopt `ubuntu-26.04` — preview
runners under the oracle violate doctrine #1) · vLLM 0.26.0, LiteLLM 1.93.0, Codex `exec --json`
JSONL shape exactly as v1.3 corrected it.

**Two assessor verdicts overturned by the judge:**

- **`postgres:18-alpine` PGDATA footgun — FIXED**, PR #1409 merged 2026-04-21. Keep the mount rule
  (`/var/lib/postgresql`, never `.../data`) exactly as written; soften only the "can silently
  re-init" wording, which is now true solely of images built before that date.
- **Classic branch protection — NOT deprecated, do not migrate to rulesets.** The assessor
  recommended moving `werftctl onboard` to the rulesets API; the judge found auto-merge is
  **documented-broken** under rulesets when required checks come from a ruleset. Staying on classic
  is correct. *(Related: a path-conditioned required-reviewer rule did ship GA 2026-02-17 — so §8.2's
  parenthetical "GitHub does not offer this" is now wrong — but it targets **teams** and is therefore
  unavailable on a personal account. Code-owner review remains the only mechanism at this tier.)*

### 15.5 — Smaller corrections worth making while building

| Item | Correction |
|---|---|
| **GitHub App installation token format** | Mid-rollout: `ghs_` + 36 chars → **`ghs_APPID_JWT`, ~520 chars, variable length**. §6.6's log-tee redaction must be **exact-value** matching — a `ghs_[A-Za-z0-9]{36}` pattern is named by GitHub as one that now fails, and a redactor that silently stops matching is worse than none |
| **Actions "spending cap"** | Renamed to **budget**, and **defaults to alert-only** — stop-usage is opt-in. A budget left at the default silently does nothing but email. Use **repository-level** budgets, which match §8.2's "per-project monthly budget line" framing exactly |
| **npm vs PyPI proxying** | Asymmetric. The npm CLI **rewrites registry hosts by default** (`replace-registry-host`), so a plain caching reverse proxy works. **PyPI does not** — `pypi.org/simple/` returns absolute links to `files.pythonhosted.org`, so proxying `pypi.org` alone is useless and that host would also need allowlisting. If a mirror is ever built, `devpi` rewrites; Verdaccio covers npm |
| **squid `ssl_bump`** | §6.7's *"full `ssl_bump` MITM"* is a straw man — **selective per-destination bump is a first-class documented feature**. The design *could* bump registry hosts while splicing provider hosts. State the real costs (CA trust, per-host config) rather than the false dichotomy |
| **ECH — RFC 9849, 2026-03-03** | Encrypted Client Hello is now Standards Track. The squid peek step can no longer *verify* that the visible SNI is the real destination. Keep the design (bumping would break the subscription CLIs' TLS), but record the residual honestly in §6.7/§13 |
| **BuildKit** | **Nine advisories in 2026**, including frontend file-escape and Git-URL path traversal. New surface created by the per-project-image decision. Write into §6.2: build inputs are human-authored only |
| **buildx `docker-container` driver** | Requires **`Privileged: true`** unconditionally (buildx's own `driver.go`). The rootless variant needs `seccomp=unconfined`. Reject both in §12 — they reopen exactly what §6.3 spends its budget closing. The default `docker` driver already gives full BuildKit through the daemon at no cost |
| **kaniko** | **Archived 2025-06-03.** Only a Chainguard fork survives. Do not adopt |
| **apko / melange** | Actively maintained, but APK/Wolfi-native against a Rocky/dnf base — a second image toolchain, fails §14. Name it in §12 so it is not rediscovered quarterly |
| **Version pins** | FastAPI 0.136.x → **0.140.x** (four minors, no breaking changes, memory-reduction work that benefits a single long-lived worker); Pydantic floor 2.11 → **2.13** (2.11 predates the 3.14 support work); state exact versions for uvicorn (0.51.x), Alembic (1.18.x), structlog (26.1.x), import-linter (2.13) and ruff instead of "current" — **an unpinned CI gate can fail on a day nothing in the repo changed** |
| **§12 revisit triggers** | The runc-escape trigger on the gVisor/Kata and `userns-remap` rejects **literally fired** (Nov 2025, again Jun 2026). Leaving it phrased as a future condition makes the ledger look unmaintained. Rewrite both to say it fired, and that the mitigation was a package upgrade — **strictly cheaper than a second runtime** — so both rejects survive |
| **Docker floor as policy, not integer** | Docker ships no LTS and no EOL calendar; a branch goes unmaintained when the next major stabilizes. `install.sh` should assert *"on the current Maintained major per moby `BRANCHES-AND-TAGS.md`"* with 29.6.2 as today's concrete value, plus a dated re-check in §13 |

### 15.6 — Two items explicitly unresolved

Recorded as unresolved rather than silently dropped:

- **containerd CVEs** (CVE-2026-50195 image-cache poisoning, CVSS 8.8, and four others) — AWS scopes
  them to the containerd CRI plugin, which Docker does not use, but that scoping could not be
  confirmed from a containerd primary source. The `≥ 29.6.2` floor pulls in the bundled containerd
  and is the practical answer; verify the bundled version at install time.
- **npm classic-token revocation** — secondary write-ups claim classic tokens are revoked and
  granular tokens now cap at 90 days. **No primary source found**, so it is not asserted. Only
  relevant if a Werft-managed project ever publishes to npm from its own workflow.

---

## 16. What "done" means

**The prototype is done when P4's acceptance passes**: a human labels an issue, an agent works it in
a contained per-project environment, an executed oracle judges it, green work auto-merges to
`unattended`, red work parks, and the human promotes.

Everything from P5 onward makes that loop **affordable** (quota), **operable** (surface),
**portable** (second provider), **durable** (continuity) and **transparent** (evidence).

The test that governs every one of them is unchanged (§14):

> **Does this keep "what merges" decided exclusively by executed checks, in one engine, with one
> source of truth, operable by one person?**
