# T7 plan-panel judgment (Fable, 2026-08-21)

Judge verdict over the three drafts in
`C:\Users\kenha\AppData\Local\Temp\claude\C--Users-kenha-Documents-git-werft\e938e188-0165-48e2-b174-0a6b75304a81\scratchpad\`:
`t7-plan-draft-concurrency.md`, `t7-plan-draft-minimalism.md`, `t7-plan-draft-testability.md`.

**Base plan: minimalism's architecture. Harden with concurrency's rigor. Fold in testability's
correctness catches.** Per-decision rulings below are BINDING for the synthesis.

## D1 — driver home: MINIMALISM wins
The attend sweep in `tick_once` is the ONLY spawn path for per-run driver tasks. Dispatch claims
and stops; attend spawns one asyncio task per `claimed`/`running` run with no live task in the
process-local registry. Recovery after crash/restart is therefore the normal path — no separate
`recover_on_start` to rot (reject concurrency's separate recovery entry point; keep its
crash-window matrix as documentation, updated to this model). Driver phases each open short
sessions; no open txn while waiting. Shutdown: stop claiming, bounded drain
(`driver_drain_seconds`, default 10 s), then cancel stragglers; **containers are deliberately left
running** (reject testability's teardown-on-cancel — killing a 60-min agent because the manager
restarted contradicts "state lives in the DB"); next boot's attend sweep re-adopts.

## D2 — claim anatomy: MINIMALISM's advisory-lock-first, CONCURRENCY's completeness
`pg_advisory_xact_lock` on the account key taken FIRST, before the candidate row lock — all claim
transactions queue in one order, deadlock structurally impossible, and at this scale (one process,
one account) the serialization cost is nil. Then candidate `SELECT ... FOR UPDATE OF runs SKIP
LOCKED` ordered `priority DESC, created_at` (matches `ix_runs_claimable`), config resolution
(missing ⇒ park from `queued`, legal edge), account resolution (missing ⇒ see D4), admission,
`attempt_no = 1 + GREATEST(COALESCE(MAX(run_attempts.attempt_no),0), COALESCE(MAX(quota_ledger.attempt_no),0))`
(concurrency's cross-table max — belt and braces for both UNIQUE(run_id, attempt_no) constraints;
NEVER `attempt_count + 1`), ledger INSERT with explicit `consumed_at = :now`, attempt seed row,
CAS `queued -> claimed` with lease/deadline/branch_name/runner_image_digest and
container_id/exit_code/base_sha reset to NULL. Blocked ⇒ CAS `queued -> blocked_quota` with
`next_attempt_at = max(retry_at, now + 60 s)` (concurrency's anti-spin floor). One candidate per
transaction; the whole unit commits once; a lost CAS rolls everything back.

## D3 — dispatch config: file, JSON, re-read per sweep
`WERFT_DISPATCH_CONFIG_FILE` → JSON `{"projects": {slug: {image_digest, model, timeout_seconds,
memory_bytes, nano_cpus}}}`, pydantic `extra="forbid"`, digest-pinned validated at load
(concurrency's validator: fail at load, not at first claim). Re-read once per dispatch sweep
(minimalism: image rebuild takes effect without restart) BUT a malformed file mid-flight keeps the
last-good config and logs an error — never crash the loop, never park runs on a typo (a malformed
file AT STARTUP fails boot loudly, testability's rule). Missing entry for a slug ⇒ per-run
PermanentError ⇒ park. Unset/absent file ⇒ empty registry, boot fine.

## D4 — provider account: settings upsert (DO UPDATE) + no-park on missing
Concurrency's lifespan upsert with DO UPDATE — settings are the single operator surface
(config + restart changes the ceiling; lowering refuses new reservations, never kills in-flight;
no psql, no script, no API endpoint) — COMBINED with minimalism's operator behavior: when no
active account exists (ceiling setting 0/unset), dispatch logs `dispatch.no_active_account` once
per sweep and returns WITHOUT parking anything — a system-wide misconfiguration is not a verdict
on any run, and parking the queue hands the operator a requeue chore. (Reject testability's seed
script; reject concurrency's park-everything.)

## D5 — run dirs: flat per-run layout (brief's recommendation)
`{runs_root}/{run_id}/` with `workspace/`, `outputs/`, `secrets/`, `task.json`. `runs_root`
default `/srv/werft/runs` (== `artifacts_root` default; warn at boot on divergence — concurrency).
T8 writes sibling `artifacts/` which T7 never creates; T7 never deletes a run directory. Prepare
is idempotent: existing `workspace/` removed and re-cloned. 0o700/0o600 modes,
POSIX-mode assertions guarded `sys.platform != "win32"`. Reject testability's per-attempt dirs
(changes T8's expected seam; name-conflict worry is handled because sweeps reap the container
before requeue). Container/network names stay per-run.

## D6 — clone: concurrency's shape, minimalism's auth
`git clone --branch <unattended> --single-branch --no-tags` (full depth of one branch — no
shallow-push edge cases, still cheap), `git rev-parse HEAD` → `base_sha` (from the clone, not the
API — kills the moved-branch race), `git checkout -B werft/run-{id}`, then GitHub-side
`ensure_branch(branch, base_sha)` + `force_reset_ref(branch, base_sha)` on EVERY attempt (SPEC
§3.2 retry = force-reset). Auth: the RUN's own transient scoped token (one mint serves manager
clone and in-box pushes — least privilege, one revoke), URL username `x-access-token@`, password
via `GIT_ASKPASS` pointing at `{secrets}/askpass.sh` (the runner's exact pattern; git-for-windows
executes .sh askpass). `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`. Token never in argv, env
of record, or on-disk git config. Failure taxonomy: non-zero git exit/timeout ⇒ infra_failure ⇒
retry ladder; repo/branch 404 ⇒ PermanentError ⇒ park — and **from `claimed` a park is two CASes:
`claimed -> failed` then `failed -> parked` (no `claimed -> parked` edge exists — testability's
catch, verify against domain TRANSITIONS)**.

## D7 — in-box credentials: converged, plus scrubbing
Transient `token_for(owner, repo, RUNNER_PERMISSIONS, transient=True)`; written by tmp +
`os.replace` into `{secrets}/git_token` (askpass re-reads per invocation). Re-mint expiry-based:
refresh when < 10 min validity remains, checked on the driver's ticker (reject fixed 2400 s
cadence); replace-then-revoke-old ordering so the mounted file never holds a dead token. Revoke
the live token in the driver's `finally` on every path; a crashed manager's token expires within
the hour (recorded, accepted). Claude credential: `WERFT_CLAUDE_CREDENTIAL_FILE` read manager-side
via `ClaudeSpec.build_env(task, credential_path=...)` → `task.json.env` (the only channel the
shipped adapter has; verified). Prompts: `{secrets}/prompt.md`, `{secrets}/system_prompt.md`.
**ADOPT concurrency's task.json scrubbing: at teardown (and by the orphan sweep) task.json is
rewritten with every secret value replaced by "<redacted>" — the retained, T8-backed-up run dir
must carry no live credential.**

## D8 — admission math: one window module, display parity, explicit now
One `quota/window.py` whose subqueries are shared with `GET /api/v1/quota` (minimalism/testability
— displayed headroom IS enforced headroom, asserted by a test). Buckets: `consumed` = Σ
actual_wallclock_s of closed entries with `consumed_at >= :now - window`; `reserved` = Σ
reserved_wallclock_s of OPEN entries, age-independent (open never ages out). All quota SQL takes
an explicit `now: datetime` parameter (concurrency's rule; the endpoint may pass its own now) —
this is what makes #24's synthetic-clock tests direct; reservations write explicit `consumed_at`.
NO Clock port (reject testability's — parameter passing suffices). Reservation =
project timeout_seconds. Refusal order: is_active false; `exhausted_until > now` (retry_at =
exhausted_until, #24 acceptance 3); provider tightening only when `last_reading_at >= now - 15 min`
(convertible: `consumed := max(consumed, utilization/100 × capacity)` — max() IS the never-loosens
guarantee, #24 acceptance 4; non-convertible: ≥ 95 blocks outright, retry_at = reading + 15 min);
window_cap_runs (count in-window entries ≥ cap blocks; retry_at = oldest_in_window + window);
else admit iff `consumed + reserved + reservation <= ceiling_seconds`. True-up at CLI exit:
attempt wall-clock via `quota.release` in finalize's txn (uncapped — honesty over neatness);
non-CLI exits true up in the SAME txn as their transition (lease-from-claimed 0; deadline/cancel
observed seconds). Release is the guarded idempotent UPDATE `... WHERE run_id=:r AND attempt_no=:n
AND actual_wallclock_s IS NULL` — first-writer-wins makes cancel-vs-finalize races safe.

## D9 — wake refinement: one headroom function, two callers
`QuotaPort` grows exactly one method (`advance_failed` signature unchanged — carried note 3);
`NullQuota` preserves today's `until or now+15min`. Real impl: durably record provider-reported
exhausted_until keeping the LATER of stored and reported (testability's never-shorten rule),
return `max(exhausted_until, earliest_headroom_at(...))` where earliest_headroom_at = the first
instant enough in-window entries age out for one typical reservation; fallback now + 15 min. The
SAME function computes the `queued -> blocked_quota` retry_at in dispatch.

## D10 — cancel: MINIMALISM's division
Route, one transaction: close open attempt (`outcome='canceled'`, ended_at, duration), guarded
true-up (0 while claimed/no container, else observed), CAS -> canceled. Route does NO Docker I/O.
The tick's canceled-container sweep kills any container belonging to a canceled run — always, even
with a live driver (the die event is what frees `await_completion`). Live driver observes die,
finds run non-running, skips finalize, tears down + revokes in its `finally`. No driver ⇒ orphan
sweep completes teardown. SPEC plan-literal recorded: canceling a running PR-less run leaves the
branch behind.

## D11 — durable exhausted_until: converged
Written via the D9 quota method in the same txn as the transition; source strings honest
(`'cli'` with parsed reset; `now + 15 min` with `'cli_no_reset'` when the CLI reported exhaustion
without a reset time — refusing to block would re-burn the account next tick). Never auto-cleared.
`alerts.quota_exhausted_until` keeps its single call site (provider-reported reset only). Per-run
UsageReport tokens/cost → `runs.result` JSONB, display-only, never admission inputs.

## D12 — sweeps: lease AND registry arbitration, concurrency's completeness
A sweep touches a run only when `lease_expires_at < now` AND its id is not in the live-driver
registry (belt and braces; on a fresh boot the registry is empty and stale leases are exactly the
dead manager's rows). Driver heartbeats the lease every `heartbeat_seconds` (30 s) to `now +
lease_seconds` (120 s). (a) expired lease, `claimed`: reap container by label best-effort, close
attempt `outcome=NULL` (kept, not deleted; no budget consumed — lease expiry is an interruption),
true-up 0, CAS `claimed -> queued`. (b) expired lease, `running`: kill/reap, close attempt
`outcome='infra_failure'`, true-up observed, CAS `running -> failed`, `advance_failed`. (c) hard
deadline (checked before lease for the same row): kill/reap, close `outcome='timeout'`, true-up
observed, CAS -> failed, `advance_failed`; map TIMEOUT -> `parked_reason='deadline'` in
`_PARKED_REASON_BY_OUTCOME` (concurrency — a deadline park must say deadline). (d) orphan sweep:
any run NOT in (claimed, running) with a labelled container ⇒ remove container + network + scrub
task.json (closes crashed-after-finalize and canceled-no-driver windows with one mechanism).

## D13 — cadence: in tick_once, concurrency's order, keep the VM knob
Order: failed wake → blocked_quota wake → lease sweep → deadline/orphan/canceled sweeps →
dispatch → attend → terminal cleanup → merging advance. (Wakes first so newly eligible runs claim
this tick; sweeps before dispatch so freed headroom is visible.) Candidates `priority DESC,
created_at`; no per-project fairness. Bound: at most `min(dispatch_max_claims_per_tick=4,
max_concurrent_runs - live_drivers)` claims per tick, stop on first blocked outcome. KEEP
`max_concurrent_runs` (default 2) — reject minimalism's no-knob argument: the quota ceiling bounds
provider-time, not VM RAM; the concurrency cap is the VM-shaped bound and lives in the claim txn
under the advisory lock.

## D14 — events/alerts: converged
No new event types, no new AlertSink methods. `run_events(event_type='dispatch')` with a `phase`
discriminator: claimed, blocked_quota, parked, workspace_ready, container_started, container_died,
lease_expired, deadline_killed, reaped, abandoned, token_reminted (payloads per drafts —
synthesize the union, keep them small). structlog names mirror phases (testability's list is a
good template). Alerts: `run_parked` at every park site; `quota_exhausted_until` unchanged.

## Additional binding decisions (from the drafts' own catches)
1. **`pushed` is manager-side**: adapter hardcodes `"pushed": false`; driver computes
   `pushed = get_ref_sha(branch) not in (None, base_sha)`. GitHubUnavailable there = leave
   `running`, let lease/re-adoption re-drive (concurrency #15).
2. **Classification inputs** (concurrency #16): exit_code = container's Completion exit code;
   envelope = `parse_stream(read_log_tail(outputs_dir)).result`; stderr = result.error.message
   or "". New bounded `read_log_tail` (last `log_tail_bytes`, default 4 MiB, drop first partial
   line, O_NOFOLLOW-consistent with read_result).
3. **Finalize re-asserts ownership** (concurrency #17): `SELECT ... FOR UPDATE` + status check
   inside the finalize txn; makes cancel-vs-finalize deterministic both ways.
4. **Close the TaskSpec drift** (minimalism/testability): `contracts/task.py::TaskSpec` gains
   `argv` + `env` — the adapter already reads both; verify against the adapter source.
5. **Acceptance realism** (minimalism): the run network is `Internal: true` with no proxy until
   T9, so a live agent cannot reach the provider API — the executed container-path acceptance
   uses a busybox-style image; the full-agent path is T9's acceptance. State this in the plan.
6. Two carried fixes (advance_merging guard; onboard constraint-name 409) land first as
   independent Part A tasks — all three drafts agree; take minimalism/concurrency's task bodies.

## Shape of the final plan
Follow the drafts' shared format (superpowers writing-plans style: goal/architecture/constraints/
research pins/behavioral decisions/file structure, then task-by-task TDD steps with failing-test →
implement → gate → commit, checkboxes, exact commands). Target the union coverage: Part A carried
fixes; Part B quota (window module + parity refactor of /api/v1/quota, admission, ledger port,
accounts upsert, wake seam); Part C runner plane (dispatch config, placement/task.json/prompts/
scrub, git clone port, credentials, read_log_tail, TaskSpec drift); Part D dispatch + driver +
sweeps + tick wiring + cancel true-up + composition root; Part E #24 acceptance (synthetic-clock
window tests, N-racer ceiling test, exhausted-until block/auto-resume, reading-never-loosens,
live-docker smoke behind @pytest.mark.docker) + full-suite verification + PR. Research pins: merge
the three drafts' tables, dedupe, keep file:line where given. Reuse the drafts' concrete test
bodies and code snippets wherever the ruling matches them — do not re-invent what a draft already
wrote well; adapt where a ruling diverges. Cross-check every ruling that cites a signature/DDL
against `.superpowers/t7/discovery-facts.json` and the checkout itself.
