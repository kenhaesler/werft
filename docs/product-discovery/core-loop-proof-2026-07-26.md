# Werft product discovery log: core-loop proof

- Date opened: 2026-07-26
- Revised: **v2, 2026-07-27** — reconciled against `ARCHITECTURE.md` v1.4 and `README.md` doctrine
- Status: Discovery paused at the user's request; **v2 resolves the reconcilable conflicts and
  re-scopes the release.** Remaining questions are in §7, prioritized and marked blocking or not.
- Discovery method: One question at a time
- First-release goal: Core-loop proof
- Pilot repository: `C:\Users\kenha\Documents\git\pcapng-inspector`
- Initial providers: Codex first vertical slice, then Claude Code against the same adapter contract

---

## 0. How to read this document

### 0.1 Precedence

This document does **not** govern. The precedence order is, and stays:

```
README.md doctrine  >  ARCHITECTURE.md  >  this discovery log
```

`ARCHITECTURE.md` line 3 already states the first half of that rule ("where this document and the
doctrine conflict, the doctrine wins and this document has a bug"). The v1 of this discovery log
was written as though it could overrule both — it contained sentences like *"the existing
architecture's rejection of mid-run checkpoint/resume must be reversed"* filed under **Confirmed
decisions**, while `ARCHITECTURE.md` §12 still said the opposite. Two documents in one repository
asserting opposite things, with no arbitration rule, is precisely the silent-drift failure §11
("Change discipline") exists to prevent.

So: **a decision recorded here is a proposal until it lands in `ARCHITECTURE.md`.** Where this log
wants something the architecture rejects, §2 states the conflict, the architecture's original
reasoning, and the disposition — accept, reject, or reshape. Nothing is reversed by assertion.

### 0.2 What changed in v2, and why

v1 was an honest capture of a good discovery session, but it was written *outward* from the user's
answers without being checked *back* against the system those answers were about. Six things
resulted, all fixed below:

| # | Problem in v1 | Fix |
|---|---|---|
| 1 | The product statement dropped the executed oracle — the one thing v1-of-the-*product* died for | §1 restored, with quota/logging kept as the top feature priorities they are |
| 2 | Three architecture rejects were reversed by assertion, with no engagement of their reasoning | §2 conflict ledger; two reshaped into cheaper mechanisms, one held |
| 3 | The quota model required provider telemetry that mostly does not exist, and the formula had no units | §3.2/§3.3 rewritten around what Werft can actually know |
| 4 | "Resume" conflated three separable things, and picked the fragile one as primary | §3.5 splits them; git commits become the checkpoint |
| 5 | Evidence capture (Playwright, screenshots, traces, full transcripts) was unbounded and uncosted | §3.6 scoped to artifact collection with hard caps and an expiry |
| 6 | "Core-loop proof" was declared, then given a scope larger than the whole architecture | §5 slices it; §4 names the precondition that gates all of it |

The user's answer record (§8) is preserved **verbatim and unchanged**. It is primary evidence; only
the interpretation around it has been corrected.

---

## 1. Product statement (corrected)

> Werft is a self-hosted agentic OS for centrally managed, unattended coding projects. **Nothing
> merges that has not been proven by an executed oracle.** On top of that gate, Werft is
> transparent and quota-aware: a user configures which provider and model performs each kind of
> work; Werft protects the user's own provider capacity by capping its own consumption, logs its
> activity and attributed usage as a first-class product surface, pauses safely when capacity runs
> out, and continues the same work afterwards without losing it.

**Why the change.** v1's statement read: *"Werft is a transparent, quota-aware orchestration system
for coding agents"* — verification appeared nowhere. That is not a wording slip; it is the exact
inversion the v1-product post-mortem warns against. `docs/lineage/v1-verdict.md` closes with:

> *"Do not start from the orchestration. Start from the oracle. […] Without that, every layer above
> is plausibility dressed as progress."*

Quota-awareness, logging and resumability are **the top-priority features** — the user said so
plainly (answers 9, 11) and this document treats them as such throughout. But they are features of
a system whose reason to exist is doctrine #1. A discovery session that asked twenty questions
about quota and none about the oracle will naturally produce a quota-shaped product statement.
That is a discovery artifact, not a decision.

### 1.1 Scope boundary (unchanged from v1 — this part was right)

Werft governs only work initiated by Werft. It does not prevent or account for the user's activity
outside Werft.

**But state the consequence honestly, which v1 did not:** on subscription plans, external activity
is *invisible to Werft until it causes a rejection*. There is no continuous "remaining quota" feed
for the CLIs in scope (§3.2). Therefore Werft cannot compute "how much is left for you"; it can
only bound "how much I take" and stop when the provider refuses. §3.3 rebuilds the reserve on that
honest footing. Any UI copy implying Werft knows the user's remaining headroom is a product bug.

### 1.2 Data boundary (unchanged — this part was right)

Werft's runtime transcripts and evidence are Werft-owned data. They must never be written into a
repository under management, nor included in its patches, commits, or pull requests.

---

## 2. Conflict ledger: this log vs `ARCHITECTURE.md` v1.4

Each row is a place where v1 of this log contradicted the shipped architecture. Disposition is
binding for v2.

| # | This log wanted | Architecture says | Disposition |
|---|---|---|---|
| C1 | Provider-reported remaining quota is authoritative; read it from the CLI or **web dashboard** via read-only automation | §12 rejects *"querying undocumented provider quota endpoints (extra ToS/stability fragility; §7 self-tracking is the sanctioned mechanism)"*; §13 #9 names provider-ToS/account-suspension as **"v2's sharpest unhedged premise-level risk"** | **Reject the scraping; keep the requirement.** §3.2 tiers telemetry: in-band CLI signals (free, sanctioned) and rejection-learned `exhausted_until` are authoritative *when they speak*; dashboard/browser automation stays rejected. The user's actual requirement is met by §3.3's self-cap, which needs no telemetry at all. |
| C2 | Quota dimensions include **tokens** and **credits** | Doctrine #3 and §7.2a: observational token counts are *"never an input to any decision"*; §12 rejects per-token cost accounting | **Reject as an admission input; accept as display.** Tokens/credits are recorded and shown (§3.9); they never gate a dispatch. Where a provider genuinely meters *only* in credits, that provider is an opaque provider (§3.4), not a token-budgeted one. |
| C3 | "The existing architecture's rejection of mid-run checkpoint/resume **must be reversed**" | §12 rejects *"mid-run CLI checkpoint/resume (per-adapter state outside the DB, anti-goal #4)"* and *"resume-in-place after quota exhaustion"*; README anti-goal: *"No state outside the database"* | **Reshape.** The user's requirement is that interrupted work is not lost (answers 8, 11) — not that provider sessions are resumed. §3.5 satisfies it with **git commits as the checkpoint**, which lives in git and the DB and violates no anti-goal. Provider-native session resume stays rejected *as a load-bearing mechanism* and is allowed only as an opportunistic optimization nothing depends on. |
| C4 | A new durable state `paused_budget` | §4.2 has a closed 10-state machine with a DB-enforced transition table, a mirrored `domain.TRANSITIONS`, a contract test asserting they are identical, and `tests/architecture_spec.test.mjs` locking the status list | **Reject the new state; reuse what exists.** Quota pause is already `blocked_quota`. User-initiated pause becomes `parked` with a new `parked_reason = 'user_paused'` — one CHECK-constraint value instead of a new state, ten new transition cells, a trigger change, a domain-mirror change and a test-lock rewrite. `parked → queued` already exists (footnote ⁹, "human requeue"). |
| C5 | Resumption is **user-controlled**; quota reset may only enable a Resume button | §7.3 + §4.2: `blocked_quota → queued` fires automatically at the computed wake time. README: Werft *"runs coding agents **unattended**"* | **Split by pause cause.** §3.5.4: quota pause resumes automatically (this is what a quota-aware scheduler *is*); every other pause class requires explicit activation. A 5-hour rolling window with `max_concurrent_sessions = 1` means quota pauses are routine — requiring a click for each would end unattended operation. |
| C6 | A per-run safety margin of 5–10 % on top of hard limits | §7.2/§7.3 already has `conservative_factor` default **0.80** — the identical concept — and §7.2a stacks `usage_limits` fractions multiplicatively on top | **Unify, do not add.** `safety_margin` *is* `conservative_factor`, exposed in the user's preferred units. Adding a third pessimism factor would silently compound: a user asking for "10 % margin" would get `0.80 × 0.90 × ceiling` — a 28 % haircut before their own ceiling applies. §3.3 collapses this to two knobs with stated composition. |
| C7 | Configuration changes take effect immediately while work is active | §5.5: `routing.yaml` is bind-mounted and reloaded **only** on explicit `POST /routing/reload` (no file-watch); DB-resident policy (`usage_limits`, provider accounts) *is* runtime-mutable | **Both are true of different things — say which.** §3.3: limits/ceilings are DB-resident and take effect on the next admission decision. Routing needs an explicit reload. Neither ever kills an in-flight run (C8). |
| C8 | (implied) Lowering a limit below current usage "takes effect immediately" | §7.2: reservations are taken inside the claim transaction and trued-up on exit; §7.3: reaching a limit is control flow, not failure | **Make explicit:** a lowered ceiling refuses *new* reservations. Reservations already held stand until the attempt ends. Werft never kills work to satisfy a config change — that would contradict this log's own decision that reaching a limit is not a run failure. |
| C9 | External publication of Werft evidence "has not yet been authorized or designed" | §10.6 already pushes pg_dump + **parked-run logs** offsite via restic nightly; §13 #12 accepts this as a stated risk | **Correct the log.** Evidence already leaves the VM by design. The open question is not *whether* but *which additional export flows* are sanctioned (§7, Q-E2). |
| C10 | Routing configures a **model** per work type ("Fable for plans, Opus for coding") | §7.1 `routing.yaml` match keys are `labels`, `language`, `size`, and rules select a **provider chain only — there is no model key anywhere in the routing table** | **Real gap in the architecture, not in this log.** §3.8 specifies the addition. The DB is already ready (`quota_ledger.model`, `usage_limits('model', …)` both exist); only the routing layer and `task.json` are missing it. |
| C11 | Routing distinguishes plans / reviews / exploration / coding / second opinion *within* a work item | §1 + §6.1: one issue = one run = one ephemeral container = one provider invocation. There is no multi-phase run model | **Not reconcilable in this release.** §3.8.2: one work unit gets one (provider, model). Multi-phase runs are a genuinely different execution model and are deferred to §5 slice 5 with the cost named. |
| C12 | Pending item: "Resolve GitHub required-check and promotion-branch contradictions identified in the current architecture" | §8.4 fixed the sync-back strict-mode inversion and §8.5 froze promotion to an immutable SHA — both labelled "(2026 currency-audit fix)" in v1.3 | **Stale item; closed.** v1 of this log was written against an earlier reading of the architecture. Removed from §7. |

---

## 3. Confirmed decisions (revised)

### 3.1 First release and pilot

1. The first release is a **core-loop proof**: one project, one provider, one full traversal of
   issue → isolated run → executed oracle → auto-merge to `unattended` → human promotion →
   sync-back. Doctrine #1 and #2 demonstrated end to end on real work.
2. `pcapng-inspector` is the pilot repository — **subject to the hard precondition in §4.**
3. The AI/Elasticsearch log-analysis project is deferred until Werft has proven its own loop.
4. Codex is the first full vertical provider slice; Claude Code is the second and must satisfy the
   same provider contract without adapter-shaped special cases.
5. Main/personal provider accounts are an intentional affordability requirement. Requiring a
   dedicated paid account solely to run Werft would defeat the point.
   **Tension to hold in view, not resolve away:** `ARCHITECTURE.md` §6.6 and §13 #9 recommend a
   *Werft-dedicated* account precisely because a ToS suspension hits the user's personal account,
   outside the VM boundary the doctrine promises. Both statements are true. The disposition is:
   personal accounts are supported and are the default; the dashboard states the suspension risk
   once, at provider configuration, in the same acknowledgement surface as §3.4; and Werft's own
   self-cap (§3.3) is the mechanism that keeps usage inside plausibly-human volumes.

### 3.2 What Werft can actually know about quota

The user's requirement (answer 23) is exact and correct:

> *"The providers reported limits are what counts! Not werfts! Its useless for me if Werft says
> 'everything is fine' and I am out of tokens for my own work, despite setting clear limits!"*

v1 of this log turned that into *"provider-reported remaining quota is authoritative"* and then went
looking for a way to obtain it — landing on reading the provider's web dashboard with browser
automation. That is the wrong shape of answer, for three reasons: the data mostly does not exist as
a continuous feed; obtaining it that way is an explicit §12 reject; and automating an authenticated
consumer dashboard from a datacenter VM sharpens the architecture's own sharpest risk (§13 #9,
account suspension). **The requirement is right; the mechanism was wrong.**

Werft's quota knowledge is tiered by what is genuinely available and genuinely sanctioned:

| Tier | Source | Availability | Status |
|---|---|---|---|
| **T0** | **Werft's own ledger** (`quota_ledger`) — runs and wall-clock Werft itself consumed | Always. Deterministic. Needs nothing from the provider | **Primary.** This is what the self-cap (§3.3) runs on |
| **T1** | **In-band provider signals** — data the CLI hands back during sanctioned work: the rate-limit stop reason, `resets_at`, and any remaining-window figure the CLI prints as part of a normal invocation | Provider-dependent, but free and zero extra exposure — it arrives on work Werft was doing anyway | **Authoritative when present.** Writes `provider_accounts.exhausted_until` (§7.3) and, where a remaining figure is given, tightens T0 |
| **T2** | **Officially documented usage endpoints** for that credential type | Rare on subscription plans; more common on API-key/Console credentials | **Permitted per-provider** where documented and supported. Never assumed |
| **T3** | Undocumented endpoints, web-dashboard scraping, driving an authenticated browser session | — | **Rejected** (§12; C1). If ever revisited it must be an opt-in per-provider flag showing the ToS risk, and it must never be a *prerequisite* for safe operation |

**The load-bearing consequence, stated plainly:** a provider that gives nothing above T0 is still
fully supported, because the self-cap does not need provider telemetry to work. Telemetry, when it
exists, only tightens the cap — it is never what makes the system safe. This is the inversion that
makes the design robust: v1 of this log made safety *depend* on telemetry it could not reliably get.

Further decisions, carried forward from v1 and unchanged:

1. Werft's internal ledger must never report that capacity is safe when the provider has said
   otherwise. `exhausted_until` always wins over the ledger's optimism.
2. External activity is never recorded as Werft consumption; it reduces real headroom and therefore
   surfaces as an earlier-than-expected provider rejection.
3. Quota capabilities are provider-specific, declared by the adapter — not forced into one
   Codex-shaped model.
4. A provider adapter declares the dimensions it can expose, from the closed set Werft meters:
   `window_runs`, `window_wallclock_s`, `weekly_runs`, plus any provider-native window the adapter
   can map onto run counts or wall-clock. **Tokens and credits are declared as display-only**
   (C2).
5. Every declared limit is independently configured and enforced concurrently. If Claude exposes
   both a session and a weekly limit, both bind.
6. The first safety-adjusted limit reached stops further work on that provider; the run falls
   through the chain (§7.3) rather than failing.
7. Quota configuration belongs to the provider/account, not to a routing rule — this matches
   §4.3 `provider_accounts` and §7.2a scopes exactly. Usage is still broken down by model, run,
   repository and work type for analysis.
8. Before a cross-provider fallback begins, the target provider must pass its own admission check.

### 3.3 The self-cap: reserve and safety margin, with units

v1 offered this formula:

```text
Werft-admissible capacity = provider-reported remaining capacity
                          - user-reserved capacity
                          - safety margin
```

It cannot be implemented as written. It has no units (remaining *what*?), it requires the T3
telemetry §3.2 just rejected, and it mixes a capacity (`reserve`) with a percentage (`margin`) in
one subtraction, so it is dimensionally unsound and behaves differently depending on which of the
two readings of "safety margin" is intended.

**Replacement.** Admission is a per-dimension check against Werft's *own* consumption, expressed as
a share of the plan. It needs no provider telemetry, and the provider's own refusal overrides it:

```text
Werft may start work on account A iff:

  (1) for every declared dimension d of A:

        werft_consumed[d] + reservation[d]
            ≤ plan_capacity[d] × utilization_ceiling × conservative_factor

  (2) and the provider has not refused:

        exhausted_until(A) IS NULL  OR  now() ≥ exhausted_until(A)
```

- `d ∈ { window_runs, window_wallclock_s, weekly_runs }` — the dimensions §7.2 already meters.
- `plan_capacity[d]` — operator-entered from the published plan (`provider_accounts`).
- `werft_consumed[d]` — a rolling aggregate over `quota_ledger`. Werft's own consumption only, by
  construction — which is exactly the "governs only its own work" boundary, mechanically.
- `utilization_ceiling` — **this is the reserve.** "Leave me 40 % of my plan" is
  `utilization_ceiling = 0.60`. Stored in `usage_limits` (§7.2a), which already supports global,
  per-provider and per-model scopes that compose by multiplication.
- `conservative_factor` — **this is the safety margin** (C6), default `0.80`, i.e. a 20 % hedge.
  The user's stated 5–10 % preference maps to `0.95`–`0.90`. Permitted range `0.50`–`1.00`.
  It hedges *measurement error* — the gap between Werft's metering and the provider's opaque
  accounting. The ceiling expresses *intent*; the factor expresses *doubt*. They are different
  things and both are needed, which is why they multiply rather than stack as a third knob.

**Composition, stated once so it cannot silently compound (C6):**

```text
effective_share = utilization_ceiling(global)
                × utilization_ceiling(provider)
                × utilization_ceiling(model, if set)
                × conservative_factor
```

Exactly one pessimism factor and up to three intent ceilings. Any future proposal to add a fourth
multiplier must state the resulting worst-case product in the same breath.

Further decisions:

1. Every enforced limit has a configurable safety margin (`conservative_factor`), per account.
2. Default `0.80`; permitted `0.50`–`1.00`. The user's 5–10 % preference is inside that range.
3. The user must see, **separately and without arithmetic**: the plan limit, Werft's consumption,
   the configured ceiling (reserve), the safety factor, outstanding reservations, and the resulting
   admissible headroom — plus, when known, the provider's own `exhausted_until` and its source and
   timestamp. §9.4's Quota page is the surface; it currently shows *used vs operator ceiling* and
   needs the remaining rows.
4. Limits are DB-resident and take effect on the **next admission decision** (C7). Lowering a
   ceiling below current usage refuses new reservations; it never kills in-flight work (C8).

### 3.4 Providers with opaque quota

Unchanged from v1 — this section was well-reasoned and fits the architecture. Restated with one
addition:

1. If a provider surfaces nothing usable, Werft must not imply its limiter protects the user's
   remaining capacity.
2. Provider configuration shows a blocking warning that Werft may consume all available quota.
3. The user may explicitly accept that risk once for that provider/account configuration.
4. The acknowledgement is stored with the configuration, not requested per run.
5. Changing the account, credentials, or telemetry mode invalidates the acknowledgement — it is no
   longer the same configuration.
6. After acknowledgement, Werft operates until the provider refuses, then pauses rather than failing.
7. **New:** an acknowledged opaque provider is still subject to the §3.3 self-cap. The
   acknowledgement removes the *guarantee*, not the *cap* — the operator can still say "Werft may
   use at most 60 % of this plan", and Werft will still honour it against its own ledger. The
   warning is that Werft cannot verify the denominator, not that the cap is ignored.

### 3.5 Continuity: pause, checkpoint, and resume

v1 filed one decision — *"the existing architecture's rejection of mid-run checkpoint/resume must
be reversed"* — as though it were a single change. It is three separable things, and v1 chose the
most fragile one as the primary mechanism.

#### 3.5.1 The three things, separated

| | What it means | Status |
|---|---|---|
| **Run continuity** | A run interrupted by a limit is not lost and eventually completes | **Already in the architecture.** `blocked_quota` → wake at reset → re-dispatch (§7.3, §4.2) |
| **Work preservation** | The interrupted attempt's *completed* work survives into the continuation | **The real gap.** §8.1 force-resets `werft/run-<id>` to `unattended` HEAD on every dispatch, so the interrupted attempt's commits are discarded |
| **Provider-session resume** | The continuation reuses the provider's own session (`claude --resume …`) | **Stays rejected as load-bearing** (C3). Optional optimization only |

The user asked for the first two (answers 8 and 11: *"stop work at any point in time and resume"*,
*"sessions can be resumed when a limit is reached"*). Nothing in the answer record asks for the
third; it was introduced by this log.

#### 3.5.2 The checkpoint is a git commit

This is the design decision v1 was reaching for and did not find.

The adapter already clones, works, commits and pushes. Make the commit the checkpoint:

- On a graceful-pause signal (SIGTERM from the manager) or a mid-run quota rejection, the adapter
  stops the CLI, **commits work-in-progress to `werft/run-<id>` with a marker trailer, pushes, and
  writes `result.json{status: "paused"}`** before exiting.
- `result.json.status` gains `paused` alongside the existing
  `{success, failure, quota_exhausted, timeout, error}`. `quota_exhausted` already exists and
  already means "stopped by a limit"; `paused` covers operator- and capacity-initiated stops.
- The manager records the pushed SHA on the run.

**Why this is the right mechanism:** the checkpoint is content-addressed, durable on GitHub *and* in
the host mirror, inspectable by a human with ordinary `git` commands, survives a `kill -9` of
manager and runner alike, and lives in git and the DB — the two places the architecture already
declares as state. It creates no per-adapter state outside the database (anti-goal #4), no sentinel
files, no `/tmp` handoffs. It is roughly ten lines in the shared adapter runtime, not a subsystem.

#### 3.5.3 Continuation is a distinct dispatch kind

Today §8.1 has one dispatch behaviour: force-reset the branch. Two are needed, and the distinction
is meaningful, not cosmetic:

- **Retry** (after CI-red or agent failure): force-reset to `unattended` HEAD, as today. Attempt
  N+1 must start clean — inheriting a failed attempt's working state is how bad work compounds.
- **Continuation** (after a pause or quota exhaustion): start from `werft/run-<id>` HEAD, and give
  the agent a compact handoff describing what the previous attempt completed and what remains.

The distinction is already available in the data: `run_attempts.outcome` distinguishes
`quota_exhausted` from `ci_red` and `agent_failure`. Continuation applies when the previous
attempt's outcome was a *pause class*; retry applies otherwise. **A continuation must not consume
the retry budget** — §5.3 already establishes that principle for provider fallthrough, and the same
reasoning applies: "budget exhausted" must always mean N genuine failed attempts, never N
interruptions.

#### 3.5.4 Who resumes, and when (C5)

| Pause cause | State | Resume |
|---|---|---|
| Quota / ceiling reached, all chain providers unavailable | `blocked_quota` | **Automatic** at the computed wake time (§7.3). This is what a quota-aware scheduler is; requiring a click per pause would end unattended operation |
| Operator pause of one run | `parked`, reason `user_paused` | Explicit — the operator resumes it |
| Operator pause of everything | `manager_state.accepting_new_runs` / `werftctl away` (§10.5) | Explicit — this is the master switch the user's answer 8 describes |
| Provider auth expired / policy block | `parked` (existing reasons) | Explicit, after the human fixes the credential |
| Corrupt or unusable checkpoint | `parked`, reason `infra_failure` | Explicit, with the branch preserved for inspection |

The user's *"resume, as soon as the user activates it again"* (answer 8) is served by the master
switch and by away-mode's rate-limited restart, not by per-run clicks. A 5-hour rolling window with
`max_concurrent_sessions = 1` produces quota pauses constantly; forty resume clicks a day is not the
product the user described.

#### 3.5.5 Provider-session resume: optional, never load-bearing

Permitted under all four conditions:

1. The session identifier is stored in the DB as an **opaque hint** on the run — Werft never
   interprets it, and no state lives outside the DB and git.
2. Resume is attempted **at most once** per continuation.
3. **Any** failure — unavailable, expired, rejected, malformed — falls through silently to §3.5.2's
   git checkpoint. Failure is normal, not an error state.
4. No correctness property may depend on it. It saves tokens and re-discovery time; it never
   determines whether work survives.

If those conditions ever look inconvenient, the answer is to drop the optimization, not to relax
them: making recovery depend on a third party's undocumented session storage is the same class of
dependency that produced v1's unverifiable behaviour.

#### 3.5.6 Retention, protection, and the expiry v1 was missing

1. Paused runs and the evidence needed to continue them are protected from routine retention GC.
2. If storage pressure threatens continuity, Werft warns and may stop accepting new work rather
   than silently deleting recovery state (§10.7's disk ladder already does the second half).
3. **New, and necessary:** paused runs **expire**. A run paused for longer than
   `WERFT_PAUSED_MAX_AGE` (default **14 days**) is parked with reason `stale_paused`; its branch is
   retained per §8.1's fixup rule but its bulk evidence returns to normal GC.

Without (3) the design has an unbounded hole: "resume is user-controlled" plus "protected from
cleanup" means a run the operator never resumes holds its workspace and evidence **forever**, on a
single 500 GB VM (§10.4). v1 noticed the pressure and specified a warning; it never closed the loop.
Every protected resource needs a release condition.

### 3.6 Durable activity journal and evidence

Logging is a defining product feature (answer 11), not secondary telemetry. Scoped so it can
actually be built:

#### 3.6.1 The one-writer boundary is not negotiable

v1 said Werft *"records enough state before and after each provider turn."* **Turns happen inside
the container**, and §4.1 states the single most load-bearing persistence rule in the system:

> *"One writer. Only the manager process holds a Postgres connection string. Runners are DB-blind."*

That rule exists because v1-of-the-product died partly of "two engines, one DB file." So:

- The **adapter emits** structured JSONL events into `log.jsonl` (already the contract, §6.3/§9.1).
- The **manager ingests** that stream — it already tails the file at 500 ms for SSE (§9.1) — and is
  the only writer of `run_events`.
- **Log content stays evidence, never a control input.** §6.4's firewall holds absolutely:
  completion authority is the `die` event plus exit code plus `result.json`, never log content.
  v1's *"restart recovery replays the durable journal"* came dangerously close to inverting this.
  Recovery replays **`runs` and `run_events`** — typed rows the manager wrote — and the log is
  human-facing evidence beside them.

#### 3.6.2 What is journalled

Per attempt boundary and per observable event, in `run_events` (small typed facts, per §4.3):
run/task/provider/model/repository identity; monotonic sequence and timestamp; routing rule and
reason; quota snapshot with telemetry tier and source (§3.2); reservation, measured usage and
true-up; git state and checkpoint SHA; provider session hint and whether resume was attempted;
limit decision and pause reason; state transitions; CI observations; continuation cursor.

Tool calls, CLI output and provider messages go to `log.jsonl` — the bulk stream, capped at 20 MB
per run (§9.1) — and are surfaced in the timeline, not stored as DB rows.

The user sees a live, replayable timeline rather than an opaque "agent is working" indicator. §9.3
and §9.4 already provide the SSE stream and the Runs timeline page; this is an enrichment of an
existing surface, not a new subsystem.

#### 3.6.3 Visual evidence: collected, not instrumented

v1 promised screenshots, Playwright actions, traces and video as flat product features. Costed
against the architecture, that is a scope explosion:

- The runner base image has no browser. Playwright adds roughly 1.5 GB and downloads browsers at
  install time from CDN hosts **not on the squid allowlist** (§6.7), which is deliberately narrow.
- Traces are tens of MB each; video more. The per-run log cap is 20 MB and the runner disk soft cap
  is 8 GB (§6.3) — against a protected-from-GC paused-run population (§3.5.6).
- Playwright evidence exists **only if the project under test uses Playwright.** It is a property
  of the project, not a capability of Werft.

**Decision: Werft collects artifacts; it does not drive a browser.**

1. A project declares an artifact directory in its onboarding config (default
   `.werft-artifacts/`, plus conventional locations such as `playwright-report/`).
2. After the run, the manager copies that directory to `/srv/werft/runs/<id>/artifacts/`, subject
   to a per-run cap (`WERFT_ARTIFACT_CAP`, default **100 MB**, truncation recorded as an event).
3. Artifacts are linked from the timeline **against the event that produced them** — the run step,
   the test invocation, the CI check.
4. Werft never installs, launches or drives a browser itself. If a project's own suite produces a
   Playwright HTML report and trace, the operator sees it. If it does not, there is nothing to see,
   and no Werft feature is missing.

This delivers what answer 12 actually described — *"track work, see the screenshots made, the
playwright being used"* — as a by-product of executed checks, which is squarely doctrine #1's
spirit, at a fraction of the cost.

#### 3.6.4 Retention and privacy: deny by default

1. All runtime evidence is local by default. It already leaves the VM in one sanctioned path — the
   nightly restic backup of parked-run logs (§10.6, accepted risk #12) — and that path is stated,
   not hidden (C9).
2. **Full prompt and response retention is off by default and opt-in per project.**
3. Usage metadata, content hashes, redacted summaries and checkpoint records are always kept.
4. **Redaction is scoped to what is achievable.** The architecture redacts the one Werft-supplied
   secret (the git token) from the log tee (§6.6); `gitleaks` in the project's own oracle turns a
   leaked credential red (§10.2). General-purpose redaction of secrets from arbitrary agent output,
   screenshots and browser traces is not a solvable problem at this scale, and pretending otherwise
   would be a false assurance. **The mitigation is deny-by-default capture (2), not
   capture-then-redact.**

### 3.7 Repository isolation

Unchanged in substance — this section was correct — with one sharpening:

1. Runtime transcripts, provider session files, artifacts, usage journals and checkpoint records
   live in Werft-managed storage **outside** the managed repository (§6.3: only `workspace/repo/` is
   mounted into the container; everything else sits beside it on the host).
2. Werft never includes those artifacts in generated patches, commits or pull requests.
3. Only deliberate project changes may enter the managed repository.
4. **The preflight scanner has exactly one well-defined job.** v1 asked for a general leak scanner
   and then filed "how to identify Werft artifacts without blocking legitimate project files" as an
   open question — a hard classification problem. It is not needed: the topology already makes
   Werft-owned evidence uncommittable, because it is not on a path the agent can reach. **One real
   vector remains**: the dispatcher writes `AGENTS.md` and `CLAUDE.md` into the working copy and
   relies on `.git/info/exclude` (§6.5), which an agent can defeat with `git add -f`. So the check
   is a deterministic path match against that known, finite, dispatcher-written set — a handful of
   lines, no classifier, no false positives on project files.
5. External publication of evidence beyond §10.6's backup path is unauthorized and undesigned
   (Q-E2).

### 3.8 Model routing

1. The user — not Werft — defines which provider/model performs each kind of work.
2. Every routing profile has a required `general` fallback. Unmatched work uses it without pausing
   (§7.1's `defaults.chain` is exactly this).
3. Werft logs the matched rule, provider/model selection and allocated budget on every dispatch.
4. Werft uses only fallbacks the user configured. With none configured, or all exhausted, the run
   pauses — Werft never substitutes an unapproved provider or model.
5. Fallback chains may cross providers. Cross-provider continuation uses a structured handoff and
   the same branch checkpoint; it never claims to resume the original provider session.
6. Handoffs carry compact goal, decision, file, diff, test and unresolved-work context — never
   replayed transcripts.
7. Model names in the answer record (Fable 5, Codex Sol 5.6, Kimi K3, Sonnet 5.0, Opus 5) are
   illustrative configuration values, never hard-coded assumptions.

#### 3.8.1 Architecture change required (C10)

`routing.yaml` currently selects a provider chain and nothing else — there is no model key in the
schema, the match keys, or `task.json`. The user's primary routing requirement is
model-per-work-type, so this is a genuine gap in `ARCHITECTURE.md` §7.1, not in this log. The
change is small and the substrate is already in place:

```yaml
rules:
  - match: { labels: [frontend] }
    chain:
      - { provider: codex,  model: <configured> }
      - { provider: claude, model: <configured> }
```

- `routing.yaml` chain entries become `{provider, model}` pairs; a bare provider string keeps
  meaning "that provider's configured default model" so existing config stays valid.
- `task.json` (§6.3) gains `model`; adapters pass it to their CLI's model flag.
- `quota_ledger.model` and `usage_limits(scope='model', …)` **already exist** (§4.3, §7.2a) — the
  per-model ceiling in the architecture is currently unreachable from routing, which is itself
  evidence the key is missing.
- Model identifiers are validated against a per-provider allowlist in `provider_accounts`, so a
  retired model name fails loud at reload rather than at dispatch (§13 #2's "model retired
  server-side" failure mode).

#### 3.8.2 What "work type" can and cannot mean in this release (C11)

**Can:** work type is derived from the issue — labels, declared language, size — and selects one
`(provider, model)` for the whole run. *"Codex for frontend work"* and *"Opus for anything labelled
`refactor`"* work exactly as the user described.

**Cannot, yet:** *"Fable 5 for plans and reviews, then Opus 5 for the coding"* within a single issue.
That requires a **multi-phase run** — several provider invocations with structured handoffs between
them inside one work item — and the architecture has no such model: §1 and §6.1 define one issue =
one run = one ephemeral container = one provider invocation. Building it means a phase state
machine, per-phase quota reservation, handoff generation and phase-level evidence.

Deferred to slice 5 (§5), with the cost named rather than smuggled in under a routing key. Note it
is not doctrine-hostile — CI still gates everything a multi-phase run produces, so no LLM opinion
enters the merge path — it is simply a large build that a core-loop proof does not need.

### 3.9 Token intelligence

1. Token awareness is a core product surface — for **auditing, forecasting and optimization**, never
   for admission (C2).
2. Werft attributes its own usage by provider, model, task, run, repository and phase wherever
   telemetry permits.
3. The activity ledger supports audit, forecasting, anomaly detection and optimization. It is not
   the authoritative account quota, and observational token counts never gate a dispatch
   (§7.3, doctrine #3).
4. Werft avoids repeatedly paying for repository discovery by persisting structured context and
   referencing durable artifacts. The dispatcher-written `AGENTS.md` (§6.5) is the existing hook.
5. Handoffs are compact by default.
6. Model selection stays governed by user routing rules. Werft never silently downgrades or
   switches models to save tokens.
7. Admission and per-turn budgeting run on §3.3's self-cap — the plan's own metered units — tightened
   by T1 signals where they exist.

---

## 4. Precondition: the pilot must be a valid oracle first

**This gates everything in §5 and is the single highest-risk item in the plan.**

`ARCHITECTURE.md` §8.6 step 3 blocks `werftctl onboard` unless `werft-oracle.yml` exists and passes
a human oracle-strength attestation. Doctrine #1 makes the oracle the only merge gate. The v1
verdict's closing instruction is *"start from the oracle."*

Inspected on 2026-07-27, `C:\Users\kenha\Documents\git\pcapng-inspector` stands as follows:

| Finding | Detail | Consequence |
|---|---|---|
| **No CI at all** | `.github/workflows/` **does not exist** | There is no oracle. `werftctl onboard` blocks. The core-loop proof cannot start |
| **No Python lockfile** | `backend/pyproject.toml` declares every dependency as a floating range (`fastapi>=0.115`, `openai>=1.51`, …); no `uv.lock`, no `requirements.txt` | The same commit can go green today and red tomorrow. §6.5's lockfile-only install policy has nothing to freeze. A nondeterministic oracle is not an oracle |
| **External system binary** | `pyshark>=0.6` requires **tshark**, not pip-installable | The oracle image needs tshark at a pinned version, or CI results vary with the runner image |
| **AI code paths in the suite** | `openai>=1.51` and `backend/app/ai/*` are under test | Any test reaching a live model makes the oracle nondeterministic, network-dependent and metered. Must be verified and stubbed |
| **Genuine asset** | 32 backend test files, real coverage of parsing, filtering, export, robustness | With the above fixed, this is a good pilot — the tests exist and appear substantive |

**Slice 0 is therefore work on `pcapng-inspector`, not on Werft:**

1. Author `.github/workflows/werft-oracle.yml` — one non-matrix job named `werft-oracle` (§8.2
   convention), `uses:` pinned to 40-hex SHAs, `zizmor`-clean, read-only `GITHUB_TOKEN`, plain
   `pull_request` trigger (§8.6 3a).
2. Lock backend dependencies (`uv.lock`, hash-verified) and pin tshark.
3. Confirm the suite is hermetic — no live model calls, no network — and stub anything that is not.
4. Fix the frontend build/lint path so it participates in the same single check.
5. Run the suite ten times on a clean runner; it must be ten-for-ten green. Flakiness in the oracle
   is indistinguishable from agent failure and will poison every measurement in the proof.
6. Add a coverage-floor or test-count-delta step (§8.2 (c)) so the `touches_tests` risk has an
   executed backstop.
7. Complete the §8.6 attestation honestly: *does it build, run the real suite, lint — and would it
   catch a plausibly-wrong change?*

Until slice 0 is done, a green Werft run proves only that Werft can move a branch — which is the
one thing the v1 verdict warns is plausibility dressed as progress.

---

## 5. Release slicing

v1 declared a "core-loop proof" and then assigned it: two provider adapters, multi-dimensional quota
telemetry, safety margins, an opaque-provider acknowledgement flow, a durable journal with a
replayable timeline, screenshot/Playwright/trace evidence with event linkage, checkpoint/pause/resume
with provider-native session resume plus handoff fallback, a leak scanner, cross-provider fallback
chains with structured handoffs, token forecasting and anomaly detection, and a dashboard — behind 44
unresolved design questions. That is larger than `ARCHITECTURE.md` v1.4 in total. A proof that
requires the whole product first is not a proof.

| Slice | Contents | Proves |
|---|---|---|
| **0 — Oracle** (§4) | Work on the pilot repo, not on Werft | The gate is real. Doctrine #1 has something to stand on |
| **1 — Core loop** | One project, one provider (Codex), one model. Issue → run → PR → green oracle → auto-merge to `unattended` → human promote → sync-back. Self-cap quota (§3.3) with **T0 only** — no provider telemetry. Journal + timeline (§3.6.1/§3.6.2). Repository isolation (§3.7) | **Doctrine #1 and #2 end to end.** Plus the user's #1 priority — quota protection — because the self-cap needs no telemetry |
| **2 — Second provider** | Claude Code against the same contract, no adapter-shaped exceptions. T1 in-band signals, `exhausted_until` learning, chain fallthrough, opaque-provider acknowledgement (§3.4) | The provider contract is a contract, not a Codex description. Quota telemetry tightens the cap it does not create |
| **3 — Continuity** | Graceful pause, git-commit checkpoint (§3.5.2), continuation dispatch (§3.5.3), `user_paused` reason, paused-run expiry (§3.5.6) | Interrupted work is preserved and continues. The user's #2 priority |
| **4 — Evidence & model routing** | Artifact collection with caps (§3.6.3), evidence-to-event linkage, `model:` in `routing.yaml` (§3.8.1), usage attribution and the quota dashboard rows (§3.3.3) | Transparency and per-work-type model selection |
| **5 — Deferred** | Multi-phase runs (§3.8.2) · provider-session resume (§3.5.5) · forecasting and anomaly detection · curated evidence export | Each needs its own trigger and its own justification |

Slice 1 alone is a meaningful, demonstrable product. **Nothing in slices 2–5 changes what merges** —
that stays the executed oracle throughout, which is why this slicing is safe to run in this order.

---

## 6. Architecture changes this release requires

Concrete edits to `ARCHITECTURE.md`, so §11's change discipline can be followed rather than
discovered later. Each is small; none moves merge authority.

| § | Change | Slice |
|---|---|---|
| §4.3 | `runs.parked_reason` CHECK gains `'user_paused'` and `'stale_paused'` (C4) | 3 |
| §4.3 | `runs` gains a checkpoint SHA and a continuation flag; `provider_accounts` gains a model allowlist and a declared-dimensions field (§3.2.4) | 3, 4 |
| §6.3 | `result.json.status` gains `paused`; `task.json` gains `model` and a continuation handoff field | 3, 4 |
| §6.5 | Adapter contract gains the graceful-pause sequence: stop CLI → commit WIP → push → write `result.json` → exit (§3.5.2) | 3 |
| §7.1 | Chain entries become `{provider, model}`; bare provider strings stay valid (§3.8.1) | 4 |
| §7.2/§7.2a | State the composition of `conservative_factor` with the stacked ceilings as one product; document `utilization_ceiling` as the user-facing reserve (§3.3) | 1 |
| §7.3 | Add the T0/T1/T2/T3 telemetry tiering (§3.2) and state that T0 alone is sufficient for safe operation | 1, 2 |
| §8.1 | Split dispatch into **retry** (force-reset) and **continuation** (start from branch head); continuation does not consume the retry budget (§3.5.3) | 3 |
| §9.1 | Manager ingests structured adapter events into `run_events`; log content remains non-behavioural (§3.6.1) | 1 |
| §9.1/§10.7 | Artifact collection, `WERFT_ARTIFACT_CAP`, and paused-run expiry `WERFT_PAUSED_MAX_AGE` in the GC and disk ladder (§3.5.6, §3.6.3) | 3, 4 |
| §9.4 | Quota page shows plan limit, Werft consumption, ceiling, factor, reservations, admissible headroom, and provider `exhausted_until` with source and timestamp, as separate rows (§3.3.3) | 1 |
| §12 | Keep the mid-run-CLI-checkpoint/resume and undocumented-quota-endpoint rejects; add revisit triggers reflecting §3.5.5's four conditions and §3.2's T3 line | 1 |
| §13 | Add: paused-run storage growth (bounded by expiry); the personal-account-vs-dedicated-account tension (§3.1.5) | 1, 3 |
| Appendix A | New tunables: `WERFT_PAUSED_MAX_AGE` (14 d), `WERFT_ARTIFACT_CAP` (100 MB), `conservative_factor` range (0.50–1.00) | 1, 3, 4 |
| `tests/architecture_spec.test.mjs` | Lock each of the above the way v1.2/v1.3 fixes are locked | all |

---

## 7. Open questions, prioritized

v1 listed 44 questions, unranked, some already answered in `ARCHITECTURE.md` (C12). Stale ones are
removed; the rest are marked **[B-n]** blocking for slice *n*, or **[D]** deferrable.

### Quota and provider contract

1. **[B-1]** Confirm the exact `plan_capacity` values for the Codex account, in the units of §3.3
   (`window_runs`, `window_wallclock_s`, `weekly_runs`). Werft's admission arithmetic is only as
   good as this hand-entered denominator.
2. **[B-2]** For each provider: precisely which T1 signals does the CLI emit on a normal
   invocation — rate-limit stop reason, `resets_at`, any remaining-window figure — and in which
   output mode? This determines whether telemetry tightens the cap or only reports exhaustion.
3. **[B-2]** What evidence accompanies a quota reading — tier, source, timestamp, account identity
   hash, window identity, reset time, confidence — and how is it shown?
4. **[B-2]** Maximum acceptable staleness for a T1 reading before Werft refuses to rely on it and
   falls back to T0 alone.
5. **[B-1]** Confirm the default `conservative_factor`. §3.3 proposes keeping the architecture's
   `0.80`; the answer record prefers 5–10 % (i.e. `0.90`–`0.95`). Which is the shipped default?
6. **[D]** How are reset times, DST changes, rolling windows and provider clock skew handled? §7.2
   uses rolling aggregates over an append-only ledger, which sidesteps most of this — confirm the
   residue.
7. **[D]** What conformance tests must a new provider adapter pass before it may claim protected
   unattended operation?

### Pause, checkpoint, and resume

8. **[B-3]** Exact graceful-pause sequence and hard-kill timeout: how long does the adapter get to
   commit and push before TERM becomes KILL? (§6.5's tree-kill gives 10 s today — likely too short
   for a push.)
9. **[B-3]** Recovery from interruption *during* a git operation or a push. Interruption during a
   file write is handled by "commit or don't"; a half-completed push is the real edge.
10. **[B-3]** Idempotency keys so a replayed continuation cannot repeat an external effect
    (duplicate PR, duplicate comment). §8.3's `ux_runs_pr` + PR adoption covers PR creation;
    enumerate the rest.
11. **[B-3]** Confirm §3.5.4's split: quota pause resumes automatically, everything else is
    explicit. This reverses a v1 decision and is the one place where a wrong call breaks unattended
    operation.
12. **[B-3]** Confirm `WERFT_PAUSED_MAX_AGE = 14 days` (§3.5.6). Any protected resource needs a
    release condition; is 14 days the right one?

### Logging, evidence, privacy, retention

13. **[B-1]** The searchable `run_events` schema and the minimum fields required for replay and
    audit (§3.6.2).
14. **[B-4]** Confirm the artifact-collection model (§3.6.3): Werft collects declared directories,
    never drives a browser. This reshapes answer 12 and should be confirmed explicitly.
15. **[B-4]** Confirm `WERFT_ARTIFACT_CAP = 100 MB` per run, and behaviour on truncation.
16. **[D]** Encryption and access controls for Werft-owned provider sessions and evidence at rest.
17. **[D]** Whether the operator can export a curated report containing selected evidence but never
    the internal transcript, and what approval that export requires (this is C9's live half).

### Routing

18. **[B-4]** Confirm the `{provider, model}` chain schema (§3.8.1) and the per-provider model
    allowlist.
19. **[B-4]** Routing-rule precedence across global, project, and one-run overrides — §7.1 defines
    project overrides as fully shadowing; confirm one-run overrides are wanted at all.
20. **[D]** The initial work taxonomy and how custom work types are added, given §3.8.2's
    constraint that a work type maps to one issue, not to a phase.
21. **[D]** Compact handoff schema, its budget, and when full context may be explicitly requested.
22. **[D]** Token-efficiency metrics and the dashboard views that make waste visible.

### Core-loop product and implementation

23. **[B-0]** Complete §4 slice 0 on `pcapng-inspector`. **This blocks everything.**
24. **[B-1]** First-release control surface: CLI-only, or CLI plus the §9.4 dashboard? The
    architecture assumes the dashboard exists; the proof may not need all five pages.
25. **[B-1]** Authentication and local security boundaries for the Werft service and browser UI —
    §5.4 (static bearer token) and §10.3 (Tailscale-only) answer this; confirm it is sufficient.
26. **[B-1]** Write the provider capability, quota telemetry, runner, journal, checkpoint and
    continuation contracts as code (`werft.contracts`), not prose.
27. **[B-1]** Define the database schema deltas (§6), API deltas, recovery matrix and acceptance
    tests for slice 1.
28. **[D]** Extend `tests/architecture_spec.test.mjs` beyond prose matching — it is currently regex
    over Markdown, which locks wording rather than behaviour. Real value arrives once there is code
    to test; until then the prose locks are doing honest work.

### Runner environment and capability

Surfaced by [`agentic-os-gap-analysis-2026-07-27.md`](agentic-os-gap-analysis-2026-07-27.md), which
found these while analysing the full-access ambition. They are **not** future concerns — each blocks
slice 1 as specified.

29. **[B-1]** **The runner cannot install project dependencies.** §6.7's egress allowlist contains
    no package registry of any ecosystem, while §6.5 *requires* `npm ci` / `uv sync --frozen`.
    Decide: extend the allowlist, or stand up a read-only pull-through mirror. Note the allowlist
    route re-opens the exfiltration channel §6.7 was built to close — a registry accepts uploads —
    which reopens §12's registry-proxy reject on a threat model it did not consider (gap analysis
    §2.1).
30. **[B-1]** **How does a project declare its system requirements?** The pilot needs **tshark**
    (`pyshark`), which is not pip-installable. §6.2's answer is a hand-edited shared base image,
    which works once and does not scale. This is the project-environment contract (gap analysis P3).
31. **[B-1]** **Per-project run concurrency.** Nothing limits concurrent runs on one project;
    `MAX_CONCURRENT_RUNS = 4` is global and `ux_runs_one_active_per_item` only guards a single
    issue. Four runs branching off `unattended` for one project generate merge conflicts the
    scheduler caused. Proposal: cap at 1 per project for the proof, so conflict rate is not
    misattributed to agent quality.
32. **[D]** Whether the agent may run the project's tests locally at all. If 29 and 30 are not
    resolved, the oracle is the agent's *only* feedback channel — survivable (the oracle is the
    gate, not the agent's local run) but it multiplies retries and CI minutes, and should be a
    stated choice rather than an accident.

---

## 8. User answer record (verbatim, unchanged)

This section preserves the user's answers that produced the decisions above.
Spelling and wording are kept as supplied where useful; short confirmations are
paired with their subject so that they remain meaningful.

1. First release: **“Number 1: Core-loop proof”**
2. Initial pilot idea: **“The pilot will be something new: An AI based log
   analysis tool for elastic. So that you can hook into an elastic stack and
   analyse it with local inference (OpenAI-Compatible API, LiteLLM and vLLM) ->
   So a completely new github project will be created”**
3. Alternative pilot: **“If you want you can use this project instead:
   C:\Users\kenha\Documents\git\pcapng-inspector -> But I wont mind manually
   validating something”**
4. Acceptance of `pcapng-inspector`: **“yeah, sure”**
5. Provider starting set: **“We start with codex and claude code”**
6. Codex-first sequencing confirmation: **“yes”**
7. Main account requirement: **“No. I will use my main one. Thats the whole goal
   of that app. It must be smart enough to track usage, so it doesnt go over user
   configured limits. Not everyone has enough financial possibilities to run
   additional accounts just for that”**
8. Scope and resumability: **“It must only govern its own usage. A user may spend
   outside of werft... Thats why Werft must be able to deal with that and be
   robust enough to stop work at any point in time and resume, as soon as the user
   activates it again, after it hit its limits”**
9. Priority of quota and tokens: **“The quota tracking feature is one of the most
   important things in this app -> Also that it is smart about token usage”**
10. Safety reserve: **“We should add a safety-margin limiter in percentage, like
    5 to 10%, before a hard limit is enforced -> User configurable”**
11. Logging and resume priority: **“But logging is the important thing, and that
    sessions can be resumed when a limit is reached”**
12. Transparent work evidence: **“I'd say so... Because it would be amazing to
    track work - see the screenshots made... the playwright being used and so
    on... So a user always sees transparently what is happening”**
13. Transcript boundary: **“Werft should make sure that the transcripts of itself
    dont land in the repositories -> They are werft only”**
14. Retention protection for paused work: **“I would say so.”**
15. User-defined routing: **“As a user we predefine what model for what work... So
    I should be able to configure Claude Fable 5 for plans and reviews, codex sol
    5.6 for frontend work and kimi k3 for second opinions for example... And then
    for exploration sonnet 5.0, for coding opus 5 and so on...”**
16. Explicit fallbacks only: **“Yes, pause. unless there is no fallback
    configured”**, clarified by **“yes”** to the interpretation that configured
    fallbacks are used and absence/exhaustion of fallbacks pauses.
17. Compact structured handoffs: **“sure”**
18. Unmatched routing: **“we configure a "general" model when it doesnt matches ->
    No pausing there”**
19. Cross-provider fallbacks: **“yes”**
20. Budget placement: **“this should be provider based”**
21. Provider-native capabilities: **“it should match the providers capabilities.
    so for example if claude has a session limit and a weekly, both should be
    configurable and respected, the same with codex, kimi, grok, gemma or others”**
22. Request for a clearer quota question: **“I dont understand your question”**
23. Authoritative quota correction: **“Important: The providers reported limits
    are what counts! Not werfts! Its useless for me if Werft says "everything is
    fine" and I am out of tokens for my own work, despite setting clear limits!”**
24. Confirmation that opaque telemetry cannot silently claim safety: **“yes”**
25. Opaque-provider behavior: **“If a provider doesnt surface the information, the
    user gets warned and must confirm, that it will use all of the quota”**
26. Confirmation lifetime: **“this is a provider configuration done once.”**

---

## 9. Resume instructions

v1's "exact next question" was:

> *When no quota API exists, may Werft read remaining quota from the provider's already-authenticated
> CLI or web dashboard using read-only automation, while logging the source and never storing the
> account password?*

**That question is now answered and should not be re-asked.** The CLI half is T1 and is already
sanctioned; the web-dashboard half is a §12 reject that would sharpen the architecture's own
sharpest risk (§13 #9), and — decisively — the user's requirement behind it is met by §3.3's
self-cap, which needs no such telemetry. Asking it again would spend a question on a choice this
document has already made with reasons.

**Resume from the blocking questions in §7, in this order:**

1. **Q23** — start slice 0 on `pcapng-inspector` (§4). Nothing else is worth doing first; there is
   currently no oracle, and without one a green run proves nothing.
2. **Q29** and **Q30** — the runner cannot install dependencies and projects cannot declare system
   requirements. These block a *useful* slice 1 even once the oracle exists, and Q29's answer
   reopens a rejected subsystem, so it needs deciding early rather than during the first run.
3. **Q11** and **Q5** — the two confirmations that change behaviour rather than detail: does quota
   pause resume automatically (§3.5.4), and what is the shipped safety factor (§3.3).
4. **Q1**, **Q24**, **Q26**, **Q27**, **Q31** — the remaining slice-1 blockers.

Running in parallel, not blocking slice 0: **D1** and **D2** of the gap analysis — which reading of
"full access" is the goal, and whether the hypervisor exposes nested virtualization. D2 is a cheap
question with a possibly-free answer that decides how expensive the whole ambition is.

Continue one question at a time. When the slice-1 blockers are settled, apply §6's architecture
deltas to `ARCHITECTURE.md` as **v1.5** with matching locks in `tests/architecture_spec.test.mjs`,
then write the slice-1 implementation plan. This log stays a discovery record; the architecture
stays the blueprint.
