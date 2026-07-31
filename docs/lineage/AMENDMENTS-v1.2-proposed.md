# Werft — Amendments to ARCHITECTURE.md v1.2

**Status:** Proposed change set, not yet accepted. Nothing here is built.
**Method:** six independent factual verifications against primary sources (GitHub REST/GraphQL schemas and docs, PostgreSQL source, gVisor source, the 2025–2026 reward-hacking literature, five mature Postgres queue implementations) and five adversarial critiques (doctrine, solo-operator operability, adversarial security, implementer, completeness). Three of the twelve proposals that opened this exercise were refuted and appear in §R (Rejected).

**Verification status — read before acting on this document.** Most claims here are verified against primary sources and cited at the point of use. Three are not, and the document's own culture requires saying so rather than letting confident prose imply otherwise:

| Claim | Status | Cost to settle |
|---|---|---|
| **A6** — adding `.github/CODEOWNERS` in a PR takes over the ownership map | **Derived, not observed.** It composes two separately documented behaviours (resolution order `.github/` → root → `docs/`, first found wins; root-anchored path matching) and GitHub's own hardening guidance prescribes the fix for exactly this reason. Nobody has watched it execute end to end. | one throwaway repo, ~10 min |
| **A3** — a check run posted cross-repo by a second App attaches to the PR head SHA and satisfies a required check | **High-confidence-by-implication.** No primary source states the head-SHA association explicitly. A3's entire mechanism depends on it. | one throwaway repo + one PR |
| **A5** — force-push re-run behaviour of a cross-repo required check | Unverified. | same canary |

Additionally: this document proposes nine structural tests (Appendix B′) and **ships none**. Until they exist it does not meet the bar it argues for — ARCHITECTURE.md's load-bearing claims are locked in `tests/architecture_spec.test.mjs`, and a document whose central argument is "lock it mechanically or it drifts back" that adds no locks is inconsistent with itself. Treat the three rows above as open items on A3, A5 and A6, not as settled findings.

---

## 0. Precedence, scope, and expiry

**Precedence: README doctrine > ARCHITECTURE.md > this document.** ARCHITECTURE.md establishes exactly two rungs and there is no third. Where this document and ARCHITECTURE.md conflict, ARCHITECTURE.md is authoritative *until an amendment here is accepted*, at which point the amendment is applied **as an edit to ARCHITECTURE.md** and reduced here to a one-line changelog stub.

**This document is transitional and must not become permanent.** A second normative document that restates schema and cites section numbers will diverge from the first within one revision, and then implementers have two blueprints and no rule for which wins. That is v1's actual disease — not bad code, but two sources of truth drifting apart while everyone believed one of them — reproduced in Markdown. It fails §14's "one source of truth" as squarely as any code change would.

Every amendment carries three fields so the fold-in is mechanical:

- **Supersedes** — the exact section and sentence being replaced, or `—` if purely additive.
- **Adds** — new sections, columns, enum values, checks, alerts, constants.
- **Tests** — which assertions in `tests/architecture_spec.test.mjs` change, and how.

Amendments are grouped by *what they protect*, not by proposal number. Each is independently acceptable or rejectable.

| Part | Concern | Amendments |
|---|---|---|
| §1 | The general law the rest obey | A0 |
| §2 | The verification layer (doctrine #1's substance) | A1–A3 |
| §3 | Merge-gate integrity (doctrine #1's enforcement) | A4–A6 |
| §4 | Dispatch correctness | A7–A9 |
| §5 | The paths nobody designed | A10–A14 |
| §6 | The one human gate | A15–A16 |
| §7 | Schema, enums, observability | A17–A19 |
| §R | Rejected, with reasons | — |

---

## 1. The general law

### A0 — The gate-provenance rule

> **Supersedes:** — (generalises the observation in §8.2 that "a same-repo PR *executes* its modified workflow before any review gate")
> **Adds:** a normative rule that every subsequent amendment is measured against
> **Tests:** assert the four legs are stated verbatim

Three separate proposals in this exercise each rediscovered half of the same requirement and each got a different part of it wrong. Stated once, it makes future gate proposals self-policing.

**Any check that gates a merge must satisfy all four legs:**

1. **EXECUTED** — it runs code and reports pass/fail. Never a model's opinion. (Doctrine #1.)
2. **UNREACHABLE** — it is produced by a workflow definition and a credential that the agent's diff cannot edit *and* the agent's runner token cannot fetch. This rules out both the target repo's own workflow files (a `pull_request` workflow runs the version in the PR's merge commit, not the base branch's) and any path inside the target repo (the runner holds `contents` write and a complete object store from `git clone --reference`).
3. **NAMED** — it is registered as a fixed-name required status check on the protected branch, so deleting it blocks the merge *by absence* rather than reporting green.
4. **DRIFT-DETECTED** — its registration is re-verified on a cadence and immediately before every merge (A4), because a required check that has been quietly de-required is not a gate.

**Anything failing any leg is not a gate. It is a report, and reports belong in the promotion PR body, never in the merge decision.**

### A0.1 — Policy is not judgment

> **Supersedes:** — **Adds:** a definition that ends a recurring argument **Tests:** none

Doctrine #1 forbids judgment in the merge path. Several amendments below introduce thresholds, and the objection "a threshold is a number someone chose, therefore judgment" will be raised. It is wrong, and the distinction is sharp:

> **A constant chosen once and applied identically to every diff, before that diff exists, is policy. An assessment formed at merge time about this particular diff is judgment. Doctrine #1 forbids the second, not the first.**

If a chosen constant were judgment, then `max_attempts = 3`, the `0.80` conservative quota factor, the 20-commit divergence banner, and every row of Appendix A would violate doctrine #1. Branch protection itself is a threshold. The line is *per-diff opinion*, and no amendment here crosses it.

---

## 2. The verification layer

ARCHITECTURE.md §8.2 accepted risk #8 states the problem honestly: the agent can weaken the suite that judges it, and **the promotion gate re-runs that same weakened suite**. The current mitigation is `runs.touches_tests` — a path flag a human eyeballs. A human eyeballing a flag is judgment, staffed by an operator whose own post-mortem records that he cannot review code in these languages. That is v1's rubber stamp relocated one level up.

The v1 verdict's closing instruction is more specific than "build a hidden test suite":

> *"Build the verifier first, prove it can tell correct from incorrect on work the agent cannot see or edit."*

**Werft implements the "cannot see or edit" half nowhere and the "prove it can tell correct from incorrect" half nowhere.** A1 supplies the proof. A2 supplies the continuous floor. A3 supplies the un-editable oracle. Each answers a different question:

| Tier | Question it answers | Cadence | Agent-editable? |
|---|---|---|---|
| T0 `werft-oracle` (exists) | does the project's own suite pass? | every run | yes — by design |
| **T1 `werft-integrity`** (A2) | did the suite get weaker, and did it actually run? | every run | no |
| **T2 `werft-holdout`** (A3) | does behaviour the agent never saw still hold? | promotion; opt-in per-run | no |
| **T3 discrimination gate** (A1) | is the suite *capable* of detecting wrongness at all? | onboarding, then scheduled | no |

### A1 — Replace human oracle attestation with an executed discrimination gate

> **Supersedes:** §8.6 step 3 ("`werftctl onboard` walks a human checklist … records the attestation")
> **Adds:** §8.7; `projects.mutation_score`, `mutation_floor`, `mutation_measured_at`, `oracle_workflow_sha`, `oracle_attested_by/at`, `oracle_attestation_expires_at`; `parked_reason='oracle_unattested'`; metric `oracle: attested | stale | unattested`
> **Tests:** assert §8.6 no longer describes attestation as the sole mechanism; assert `oracle_unattested` is in the `parked_reason` CHECK list

**Defect.** §8.6 concedes that "the system mechanically cannot judge whether the oracle is real (an `echo ok` workflow satisfies every automated check and voids doctrine #1)" and answers with a human checklist. Two consequences the shipped document does not draw:

1. **The attestation is judgment in the exact position doctrine #1 reserves for execution.** It is the same structural shape v1 died of, moved one rung up.
2. **The consequence of being unattested is a dashboard banner while the project keeps dispatching and auto-merging.** Doctrine #1's second sentence is "red or **untestable** work parks for a human." A project with no attested oracle is the definition of untestable, and today it merges. The letter is satisfied; the intent is voided.

Also: `oracle_attested_at` is stamped once and nothing ever invalidates it. A suite gutted by two hundred individually-green PRs still reads "attested" a year later.

**The v1 verdict names the fix.** Its "honestly-viable survivor" gate is *"sandboxed test execution + SAST/secret/dependency scanning + **a mutation-score threshold**."* Werft implements the first, gestures at the second (§10.2's gitleaks recommendation), and implements none of the third. Mutation testing measures precisely "does this suite catch a plausibly-wrong change" *by making plausibly-wrong changes* — executed, deterministic, no opinion anywhere.

**Change.**

1. **Discrimination measurement.** A `werft-oracle-mutation` workflow runs in GitHub Actions (never on the VM — no second execution engine), on a weekly schedule and on demand from `werftctl`, using the ecosystem's standard tool (`mutmut`/`cosmic-ray`, Stryker, `go-mutesting`, `cargo-mutants`), scoped to files changed since the last measurement to bound runtime. It writes `projects.mutation_score`.

2. **Attestation becomes a measurement, not an opinion.** A project is attested when its suite demonstrably kills seeded faults at or above `projects.mutation_floor` (default `0.60`). The human checklist survives only as the *scope* declaration (which paths are the suite, which tool). The certification is executed.

3. **Attestation binds to content and expires.** `oracle_workflow_sha` pins the attested `werft-oracle.yml` blob and is compared on the existing 5-minute poll; any change — including one the operator merged himself, or one arriving via sync-back from `main` — sets `oracle_attested_at = NULL`. Independently, attestation expires after 90 days.

4. **Unattested is a dispatch bar, not a banner.** The dispatcher refuses to claim runs for a project whose attestation is NULL, stale, or below floor; the run parks with `parked_reason='oracle_unattested'`. This introduces no judgment into the merge path — it *withholds automation from projects where the executed gate has not been shown to exist*, evaluated before any merge decision exists.

5. **Oracle content floor at onboarding.** Onboarding parses `werft-oracle.yml` and requires it to invoke a test runner, a linter, a secret scanner, and the ecosystem's dependency audit. Re-verified whenever `oracle_workflow_sha` changes. An `echo ok` workflow currently satisfies every automated check in the system; this is what stops it.

6. **Enforcement placement.** Mutation score below floor blocks **promotion** (409 with the score), not individual runs landing on `unattended` — doctrine #2 already contains those, and a slow signal belongs on the slow gate.

**Residual.** Mutation score measures the suite, not the diff. It is gameable by a suite that kills mutants in trivial code while ignoring the hard paths. It raises the floor; it does not certify correctness.

### A2 — `werft-integrity`: a required, un-editable suite-regression check

> **Supersedes:** §8.2(c) ("projects are **encouraged** to include an executed coverage-floor/test-count-delta step")
> **Adds:** required check `werft-integrity`; `runs.test_count_base/head`, `runs.skipped_delta`; `projects.test_count_floor`; `parked_reason='test_integrity'`; `deploy/oracle-templates/<language>.yml`
> **Tests:** assert `werft-integrity` is named as a fixed check; assert the new `parked_reason` is in the CHECK list

**Defect.** "Encouraged" is not a mechanism. And the naive form fails A0's legs 2 and 3.

**Change.** A fixed-name required check, **not a step inside `werft-oracle`** — a step that reports green when deleted is not a gate; a required check that never reports blocks the merge by absence.

- **Two jobs, not one.** `baseline` checks out `github.event.pull_request.base.sha` and emits counts as job outputs; `head` measures the PR head. HEAD-side collection code cannot rewrite `needs.baseline.outputs`. (In one job, the agent's own collection code executes with write access to the file holding the base number, and the "baseline computed in CI, never read from a repo file" guarantee is void.) Name the base explicitly: after `update-branch`, `HEAD^1` is the previous head, not the base — a guaranteed implementer error.
- **What is measured:** collected test count delta, and **skipped/xfail count delta**. Both via `--collect-only`-class commands — cheap, no base build required.
- **Liveness floor.** The check asserts `tests_collected > 0` on both sides and fails closed when base collection errors or returns zero. A vacuous `0 → 0` delta must be red.
- **Absolute floor, not only relative.** In `strict_serialized` the base of a run PR is `unattended` HEAD, which already contains every previously-accepted small drop. Twenty runs each shaving 0.4% under a 0.5% threshold silently remove 8% of the suite with every individual check green. `projects.test_count_floor` is captured at attestation, stored in the manager's DB (never a repo file), and ratchets upward only when a promotion lands green.
- **Harness sovereignty.** The runner config, plugin set, and invocation command come from the fixed template, not the repo: explicit config path, `--rootdir` outside the repo's config, plugin autoload disabled, no repo-defined script names (`npm test`, `make test`).
- **No retry.** An integrity failure is deterministic. Retrying it three times trains toward padding and burns three chain cycles on a certain red. `parked_reason='test_integrity'`, retry budget zero.

**Coverage delta is deliberately excluded** — see §R.3.

**Residual, stated plainly so nobody leans on this harder than it deserves:** test count is an agent-controlled number in every mainstream harness (`conftest.py`, `addopts`, `testpaths`, `pytest_generate_tests` — a 3-case parametrize becomes 300). And **deleting the assertions inside existing tests leaves both metrics numerically identical.** A2 is a tripwire against blunt deletion, not a proof of suite strength. A1 is the non-gameable half; the two are complements.

### A3 — `werft-holdout`: an oracle outside the agent's reach

> **Supersedes:** §8.6's App permission ceiling sentence ("Nothing may ever require more") — explicitly, deliberately, once
> **Adds:** §8.8; a second GitHub App identity; `projects.holdout_repo`, `holdout_pinned_sha`, `holdout_rotated_at`; `runs.holdout_status`; `promotions.holdout_status`; `parked_reason='holdout_red'`; `run_attempts.outcome='holdout_red'`; alert A8
> **Tests:** assert `werft-holdout` is a fixed name; assert §8.2's "no repo/org secrets exposed to the oracle workflow" sentence survives unmodified; assert promotion required checks include the holdout

**The obvious design is unsafe and must be named as such**, because an implementer will reach for it: putting the holdout tests in a private repo and having `werft-oracle.yml` fetch them requires a secret in the very file §8.2 forbids secrets in, *for the reason §8.2 itself states* — a same-repo PR executes its modified workflow before any review gate. Run PRs are same-repo branches, so they receive full repository secrets (the fork-PR secret isolation everyone half-remembers does not apply). CODEOWNERS blocks the **merge**, never the **execution**. The first agent PR reads the holdout credential out of the environment, and hosted runners have unrestricted egress. The in-repo variant is worse: `git clone --reference <mirror> --dissociate` yields a complete object store, so any excluded path is recoverable with `git show`.

**Invert the topology.** The holdout runs the target; the target never runs the holdout.

```
  manager ──repository_dispatch(target_repo, head_sha)──► holdout repo
                                                            │
                                    ┌───────────────────────┴───────────────────┐
                                    │ job: build   (no secrets, permissions:{}) │
                                    │   checkout target @ head_sha              │
                                    │   install deps with scripts disabled      │
                                    │   build → artifact                        │
                                    ├───────────────────────────────────────────┤
                                    │ job: probe   (no secrets, permissions:{}) │
                                    │   download artifact + holdout suite       │
                                    │   run product as SUBPROCESS / container   │
                                    │   probe it across a process boundary      │
                                    ├───────────────────────────────────────────┤
                                    │ job: report  (needs: probe)               │
                                    │   holds statuses:write for target repos   │
                                    │   posts `werft-holdout` from needs.result │
                                    └───────────────────────────────────────────┘
                                                            │
                              required check on `unattended` and `main`, App-pinned
```

**The five properties that make this work, each load-bearing:**

1. **The holdout repo is never onboarded to Werft and the Werft App is not installed on it.** Unreachability is a *credential capability*, not a policy. No installation token can read it.
2. **The `report` job derives its verdict from `needs.probe.result`** — a value set by GitHub's runner infrastructure, not by the job's own code. Target code that hijacks its own process (`atexit` → `os._exit(0)`, patched assertions, a `pytest_runtest_makereport` hook forcing `passed`) cannot report itself green.
3. **The probe is out-of-process, and this is a hard requirement, not a style note.** A unit-level holdout must import the code it judges into its own test process — and the target's module-level code runs before any assertion, where `sys.settrace`, a `conftest.py`, a `sitecustomize.py`, a `pytest11` entry point, a `jest` setup file, or `NODE_OPTIONS=--require` defeats every assertion without the agent ever seeing a holdout line. **A holdout suite must therefore be black-box: it exercises the product across a subprocess or localhost-HTTP boundary and does all comparison in holdout-owned code.** The honest consequence: *code with no process or network boundary cannot be held out this way.* That narrows which projects are eligible — and the narrowing is the feature, matching the v1 verdict's "only in repositories with strong existing test suites."
4. **Required check, App-pinned.** Branch protection can require a named check *and* pin the producing GitHub App, so a job in the target repo cannot forge `werft-holdout`. Holdout unavailability is RED, never skipped — a skippable required check is not a gate (see A5). **Unverified dependency:** that a check posted cross-repo by the reporter App attaches to the PR head SHA and thereby satisfies the required check (§0). No primary source states this; the whole mechanism rests on it. Canary before building.
5. **Permission ceiling amended explicitly.** Posting a check inward needs `statuses: write`. §8.6 says "Nothing may ever require more." That sentence is amended *by naming a second, separately-scoped identity* — the Werft App's own ceiling is unchanged; the holdout reporter is a distinct trust domain holding `statuses: write` and nothing else.

**Secrecy is a non-goal. Say so.** Every structural fix degrades but cannot eliminate leakage, because the holdout must execute against agent-authored code on a network-unrestricted hosted runner. Writing the spec as though the holdout is secret means the first leak — which will be silent — voids the gate while every dashboard indicator stays green. This is empirically the failure mode: in the only production-scale test of a hidden oracle, **415 of 429 successful traces from the top Terminal-Bench 2.0 submission read the supposedly-inaccessible `/tests` directory, often as their first action** (Meerkat, arXiv:2604.11806). And a benchmark-harness audit achieved **241/241 tasks "verified" by replacing `/usr/bin/curl` so the harness downloaded a trojanized installer** — the agent never read a test; it owned the interpreter that ran them (BenchJack, arXiv:2605.12673).

> **New §13 accepted risk:** *The held-out suite is un-editable, not un-readable. Assume its contents are recoverable by a determined agent. Its value is that the agent cannot change it, cannot change the harness that runs it, and cannot make it report green.*

**No retry, ever.** A holdout red takes `awaiting_ci → parked` with `parked_reason='holdout_red'`, consuming no retry budget. Retrying an invisible oracle is a slot machine that leaks one bit per attempt, reconstructing exactly the visibility the mechanism exists to deny. **And no holdout failure detail — test names, assertion text, diffs, counts — is ever written into `task.json`, a PR body, or any surface a later run can read.**

> **This makes an existing design decision newly load-bearing.** §8.1's force-reset-every-attempt and §1 step 5's fresh-dispatch-on-red already mean CI output never re-enters an agent's context. That is what closes the pure-feedback channel — the one a hidden suite cannot close by hiding, since an agent iterating against pass/fail alone is detectable as test-gaming by submission round two (CapCode, arXiv:2606.07379). **Promote "CI output never re-enters an agent's context" from an implicit consequence to a stated invariant**, because A3 depends on it.

**Acceptance is executed, not reviewed.** The clause "agent-generated once, then human-reviewed and frozen" is **rejected** — it reinstates the exact rubber stamp v1 died of, staffed by the same operator. Instead: generation may be agent-assisted and out-of-band (never as a Werft run — `runs.backlog_item_id NOT NULL` makes that structurally impossible anyway, and a run authoring the oracle is an agent editing Werft's verification substrate); **acceptance requires the candidate holdout to demonstrably kill seeded faults** via A1's discrimination gate, plus a green pass on unmodified HEAD. A holdout that cannot fail is an `echo ok` workflow with extra steps.

**Scope, so it does not become self-defeating.** Restrict the holdout to invariants that are contractually not allowed to change: it builds, it boots, it responds on its declared entrypoints, its public API/CLI contract round-trips, the specific bug an issue was filed for stays fixed. **Golden-master snapshots of business behaviour are forbidden** — Werft exists to change behaviour, and a suite that fails on every legitimate change hands the operator precisely the judgment call the v1 verdict says he cannot make.

**Amendment ceremony.** Holdout changes land only as PRs in the holdout repo, recorded with sha/reason/human, resetting `holdout_pinned_sha` and `oracle_attested_at`. The amendment *rate* is a metric: operator-side holdout weakening is the same reward hack with a human in the loop.

**Cadence.** Mandatory at the promotion gate. Opt-in per project for run PRs (`projects.holdout_required_sizes`, default empty). The originally-proposed "mandatory for `size:large`" is **rejected**: `size` is a human-supplied label with a bypass-by-default value, so the gate is triggered by a field controlled by the same person it inconveniences — and worse, `size` is read from a GitHub-mirrored field the agent's token can edit (A9, A10).

**On the literature, cited correctly.** SpecBench (arXiv:2605.21384v1) finds the visible-vs-holdout gap grows ≈**27** percentage points per tenfold increase in reference-implementation LOC (R² = 0.21; the abstract's "28" contradicts the body — cite 27). **It must not be cited as evidence of deliberate tampering:** its own Fig. 9 finds "deliberate exploits are rare, while compositional failures account for a much larger fraction," and its dominant category is explicitly *not* intentional. The correct reading is stronger for Werft's purposes, not weaker: **the holdout catches green-but-wrong regardless of intent.** Note also the honest floor — a 186K-line human-supervised compiler passing the full GCC torture suite still shows Δ = 14.5pp. **A holdout used as a binary auto-fail will block honest work.** This is why the holdout parks for a human rather than failing a run into oblivion, and why it lives primarily on the gate where a human is already present.

---

## 3. Merge-gate integrity

### A4 — Verify the merge gate's preconditions before every merge, not once at onboarding

> **Supersedes:** §8.6 step 5 ("The App holds `administration:read` to detect protection drift afterwards (dashboard banner…)")
> **Adds:** `projects.protection_fingerprint`, `protection_verified_at`, `protection_drift_detail`; poller entry (5 min); alert A9; §11 rehearsal step
> **Tests:** assert the fingerprint field list is enumerated

**This is the largest prose-claim-with-no-mechanism in the shipped document, and it sits directly under doctrines #1 and #2.** The drift banner has no column, no entry in §8.3's enumerated poller list, no row in §9.5's alert table, and no test. So the configuration that makes "green CI" mean "green CI on the merged result" — strict mode, the required check set, code-owner review, include-admins, no-force-push — is applied once by a PAT that is then discarded and **never checked again**.

Every one of these lapses in real life: a repo transferred loses protection rules; a required check renamed by a legitimate PR stops being required; someone disables strict mode to unstick a PR at 2 a.m.; a plan downgrade removes the feature entirely (protected branches and CODEOWNERS are **not** available on a Free personal account's private repos — see A6). In every one of those worlds Werft keeps merging, believing an oracle that is no longer required.

**Change.** A stable hash over the load-bearing facts for both branches: required status-check contexts (must contain `werft-oracle`, `werft-integrity`, and where configured `werft-holdout`), the strict/up-to-date flag, required approving review count, require-code-owner-reviews, **dismiss-stale-reviews**, `enforce_admins`, `allow_force_pushes`, `allow_deletions`. Captured by `werftctl onboard` as attested-good. Enforced in three places:

1. a 5-minute poller entry (new row in §8.3's list);
2. **synchronously inside the merge handler, immediately before the squash-merge call** — mismatch aborts the merge and parks with `merge_blocked`, no retry;
3. at promotion open, blocking the batch PR.

Mismatch sets `projects.is_paused = true` (existing mechanism, no new state) and fires **A9 protection drift**, critical, no cooldown, naming the exact fields that changed. Un-pausing requires `werftctl reattest-protection` under a human identity.

**Rehearsal (§11):** flip strict mode off on the sandbox repo and assert the next merge **parks instead of merging**. That is the one rehearsal proving doctrine #1's enforcement is real rather than merely configured — the same argument §10.6 already makes for backups.

### A5 — Check identity: skipped is not green

> **Supersedes:** §8.3's singular "open run-PR check/merge status (30 s)"
> **Adds:** `projects.required_checks TEXT[]`; poller assertions; `parked_reason='check_missing'`, `parked_reason='oracle_ambiguous'`; alert A10
> **Tests:** assert the four assertions are enumerated; assert both new reasons are in the CHECK list

Werft's entire merge decision reduces to "is the check named `werft-oracle` green on this SHA," and that predicate is ambiguous and spoofable in ways the document never addresses. A2 and A3 make it worse by turning one check into three.

**Four assertions, all pure and unit-testable, added to the §8.3 poller contract:**

1. **Identity.** The check is matched by `(check_suite.app.id, workflow path, name)`, not by name alone. Check-run names are **not** namespaced by workflow: an agent PR adding `.github/workflows/x.yml` with a job named `werft-oracle` running `true` produces a second success-reporting check run of the required name on the same SHA. CODEOWNERS on `/.github/workflows/` happens to block that PR's *merge* today — the spec survives by luck, not by design.
2. **Conclusion.** `success` and nothing else. `skipped`, `neutral`, `cancelled`, `stale`, `action_required` are **red**. Branch protection accepts a `skipped` required job as satisfied, and job-level `if:` and workflow-level `paths-ignore` are both agent-reachable — so `paths`/`paths-ignore` filters and job-level `if:` are **banned** in the oracle, integrity, and holdout workflows, validated at onboarding.
3. **Multiplicity.** More than one check run matching a required name on a SHA is red, with `parked_reason='oracle_ambiguous'`.
4. **SHA binding.** The check's `head_sha` must equal the PR head at merge time.

**Aggregation.** Green means *every* name in `projects.required_checks` reported success. A required check that never reports gets `parked_reason='check_missing'` and alert A10 — replacing today's misleading 6-hour `ci_timeout` park, which is the fail-closed path but with the wrong reason and the wrong latency.

### A6 — Fix the CODEOWNERS configuration: it is currently bypassable in one PR

> **Supersedes:** §8.2's "A `CODEOWNERS` file assigns `/.github/workflows/` and `/CODEOWNERS` to Ken"; §8.6 step 2's protection list
> **Adds:** corrected CODEOWNERS spec; harness path coverage; plan-availability statement; onboarding post-conditions
> **Tests:** assert the file lives at `.github/CODEOWNERS` and that `/.github/` is owned wholesale

**This is an exploitable defect in the shipped spec, not a refinement** — derived from documented behaviour but **not yet observed end to end** (§0, verification status; settle it with a throwaway repo before treating it as established). GitHub resolves CODEOWNERS from `.github/`, then the root, then `docs/`, **using the first file it finds**. §8.2 owns `/CODEOWNERS` (root-anchored) and `/.github/workflows/`. Therefore:

> An agent PR that **adds a new `.github/CODEOWNERS`** matches neither owned path. It is not code-owned, so it merges review-free at 0 required approvals — and on merge that file wins precedence and **silently replaces the entire ownership map.** One PR, complete takeover of the control that protects the oracle.

**Corrections, all mandatory:**

1. **File at `.github/CODEOWNERS`; own `/.github/` wholesale** (which subsumes `/.github/workflows/` *and* `.github/actions/**` and root `action.yml`, which composite actions use and which the current glob misses entirely). Defensively also own `/CODEOWNERS` and `/docs/CODEOWNERS`.
2. **Own the harness, not the tests.** §8.2 correctly refuses CODEOWNERS on test directories ("good PRs touch tests — killing unattended operation") and then protects only the workflow, leaving the entire layer *between* the workflow and the tests unprotected. The workflow says `make test`; the workflow is protected, the behaviour is not. Own: `conftest.py`, `pytest.ini`, `tox.ini`, `noxfile.py`, `setup.cfg`, `pyproject.toml`, `Makefile`, `justfile`, `package.json`, `jest.config.*`, `vitest.config.*`, `.mocharc*`, `.rspec`, `go.mod` — plus registry/toolchain config `.npmrc`, `pip.conf`, `.netrc`, `.tool-versions`, `.nvmrc`. **Accept the cost explicitly:** `pyproject.toml` and `package.json` are touched by legitimate dependency work, so those become human-review PRs. That is a strictly better use of the one human gate than eyeballing a flag *after* the merge to `unattended` already happened.
3. **Enable "Dismiss stale pull request approvals when new commits are pushed."** Without it a code-owner approval on attempt 1 survives attempt 2's force-push of entirely different content — defeating the self-protection mechanism in exactly the case it exists for. Leave "Require approval of the most recent reviewable push" **off**; it imposes an approval requirement on all PRs and breaks the review-free path.
4. **Identity.** GitHub Apps **cannot** be code owners (`app/foo` and `@foo[bot]` are lint errors), and a PR's author can never approve it. If Werft ever opens PRs with the operator's PAT, the operator becomes the author and can never approve a code-owned PR — an unbreakable deadlock on a personal repo with no second owner. Werft must act as a GitHub App installation identity, always.
5. **Availability, which the shipped document omits and which bites Werft's stated target.** CODEOWNERS, protected branches, and rulesets are free only on **public** repositories for a Free personal account. On a personal account's **private** repo all three require **GitHub Pro**. No organization is needed, but bypass lists, "restrict who can dismiss reviews," and the team-based Required Reviewers rule are org-only and therefore unavailable. State the plan requirement in §10.8 step 2.
6. **Onboarding proves it, by executing it.** Werft's own doctrine is that verification is executed, never assumed — that must apply to Werft's own protection configuration, currently the least-verified load-bearing component in the system. A CODEOWNERS line naming a user without write access on that repo is **silently ignored, per line, with no UI error**. So: call `GET /repos/{o}/{r}/codeowners/errors` and require zero (re-run in A4's drift check); then create a throwaway branch touching `.github/workflows/werft-oracle.yml`, open a PR, assert `mergeable_state == 'blocked'` and that a merge attempt returns 405, then close it. *A protection rule that has never been observed to fire is not a protection rule.*

---

## 4. Dispatch correctness

### A7 — `MAX_CONCURRENT_RUNS` has no enforcement point; the fix is not a global lock

> **Supersedes:** §10.4 ("enforced in the dispatcher against the DB"); §5.1's conflation of the worker pool with the cap; §5.1's "documented, **not built**" scheduler election
> **Adds:** `WORKER_COROUTINES` as a distinct constant; `provider_accounts.lock_key`; namespaced advisory-lock keys; metric "dispatch slots in use"
> **Tests:** assert Appendix A carries two distinct constants

**Defect confirmed.** §10.4 asserts the cap is "enforced in the dispatcher against the DB"; §7.2's claim SQL contains no global predicate; and §5.1's fixed pool of `MAX_CONCURRENT_RUNS` coroutines cannot substitute, because §5.2 mandates every `advance()` handler be **short-lived and return** — so a run occupies no coroutine while its container runs. The cap is enforced nowhere. Unenforced, §10.4's sizing (4 runners × 2 vCPU/4 GB inside 16/64) is fiction: 16 concurrent runs exceed host RAM, the OOM killer selects among the largest RSS processes, and the realistic casualty is **Postgres — the queue, the event bus, the metrics store and the single source of truth, all at once.**

**But the proposed fix — a global advisory lock inside the claim transaction — is wrong**, and the survey of mature Postgres queues says why: **not one of River, Oban, graphile-worker, procrastinate, pgmq or pg-boss enforces a concurrency cap that way.** River bounds the `SKIP LOCKED` fetch by an in-process counter (`MaxWorkers - numJobsActive`); River Pro and Oban Pro needed dedicated coordination tables *only because they support multiple client processes*, which Werft forbids by doctrine.

**Correct decomposition:**

| Concern | Mechanism | Why |
|---|---|---|
| **Singleton** | promote §5.1's session-level `pg_try_advisory_lock` election from "documented, not built" to **built** | guarding against an accidental second manager is the only thing a *database-level* global cap actually buys |
| **Global cap** | in-process semaphore, surfaced into SQL as a fetch bound so a run with no free slot is never a claim candidate | exact, zero DB cost, zero contention — Werft is single-process by doctrine |
| **Per-account cap** | keep the existing `pg_advisory_xact_lock` in the claim transaction | genuinely load-bearing |

**This preserves a structural property the global lock would destroy.** §7.2's "one candidate per transaction, strictly" already bounds the hold-set to **one** lock, and a hold-set of size one has no ordering to get wrong. Adding a second lock trades a structural guarantee for a convention that must be policed forever.

**Four implementation traps, each verified against PostgreSQL source, that the spec must state:**

1. **A transaction-level advisory lock is released by a SAVEPOINT rollback, mid-transaction.** Non-session locks attach to `CurrentResourceOwner`, and a subtransaction abort releases them while the outer transaction continues. SQLAlchemy `begin_nested()`, a savepoint-based retry wrapper, or a PL/pgSQL `BEGIN…EXCEPTION` block **silently voids the mutual exclusion the whole design rests on.** Mandate: acquire the lock at the top level, before any savepoint; ban savepoints in the claim path.
2. **Stop hashing.** `hashtext` returns int4 and is an undocumented internal whose output is not stable across major versions. The real hazard is not intra-class collision but **cross-class collision with the session-level election key** — a session lock held for the process lifetime blocks a transaction lock on the same identifier *forever*. Add `provider_accounts.lock_key INT GENERATED ALWAYS AS IDENTITY` (~4 rows) and use the two-int32 form with explicit namespaces: `(1,0)` global, `(2, lock_key)` per account, `(3,0)` election. The one-arg and two-arg keyspaces provably do not overlap.
3. **No transaction holding the per-account lock may ever WAIT on a `runs` row lock.** The dispatch transaction holds a `runs` row lock (via `SKIP LOCKED`, which never waits) *then* waits on the account lock; any counterpart path that takes the account lock and then waits on a `runs` row lock closes a cycle. Werft has two such candidates today: the `actual_wallclock_s` true-up and the `exhausted_until` write. Advisory locks *do* participate in deadlock detection, so this aborts with 40P01 rather than hanging — but it is a real abort in the merge path.
4. **The claim transaction contains SQL only.** §1 step 2 currently reads as though branch creation and container launch may be inside it. State that they occur strictly after COMMIT — one slow Docker call inside the transaction stalls dispatch for the entire system.

**The cap counts `status IN ('claimed','running')` and nothing else** — `awaiting_ci` runs hold no container. And the cap is only as honest as the reaper: a leaked `running` row permanently burns 1/4 of global capacity, and four leaks stall every project **while nothing is failing and no existing alert fires.** Pair with A18's stall alert and a reconciliation sweep that lists containers by `label=werft.run_id`, removes any with no live run row, and alerts if the Docker count exceeds the DB count.

### A8 — Saturation is not exhaustion

> **Supersedes:** §7.2's four-predicate guarded INSERT; §7.3's wake-time computation; §4.2 footnote ᵃ
> **Adds:** `decide_dispatch` return type; `run_events.event_type='dispatch_deferred'`; `WERFT_SATURATION_DEFER_SECONDS`; `run_attempts.chain_position`, `selection_reason`; A2 alert extension
> **Tests:** assert `blocked_quota` is documented as reachable only from genuine exhaustion

**Defect.** §7.2's single guarded INSERT returns zero rows for **four distinct reasons** and the caller cannot tell which. §7.3 then treats every zero-row result as quota exhaustion and computes a wake time from `exhausted_until` or the rolling-window edge — hours, or in §5.2's own words "week-long quota blocks" — when the actual wait may be the **minutes** until a peer run finishes. With `max_concurrent_sessions` defaulting to 1 on all four providers, this is not an edge case; it is close to the steady state.

**Root cause, which the fix must address rather than paper over:** `max_concurrent_sessions` is a **live-occupancy** property of `runs`, while the other three caps are **historical-consumption** properties of the append-only `quota_ledger`. Folding a live-occupancy predicate into a ledger INSERT is what makes saturation indistinguishable from exhaustion — *and* what leaves it unprotected by the per-account lock (the predicate keys on `provider` while the lock keys on `account`, a latent correctness hole §7.2 already concedes in a parenthetical).

**Change.**

1. **Lift the session-concurrency check out of the guarded INSERT** and enforce it as a pre-transaction fetch bound. A saturated run is then never a candidate: no transaction opens, no lock is taken, no state is written, no `quota_ledger` ghost reservation lingers.
2. **Name the return type.** `decide_dispatch` today is typed `→ {selected provider | park}`. It becomes `Dispatch(provider) | Saturated(retry_after) | Exhausted(wake_at) | Park(reason)`. Every consumer changes; say so.
3. **Deferral leaves the run in `queued`** with `next_attempt_at = now() + WERFT_SATURATION_DEFER_SECONDS` (default 15 s, matching the tick). No status change, no `run_attempts` row, no `attempt_count` bump.
4. **Tick-only wake. Drop the NOTIFY claim.** The original proposal said "woken by the completion notification"; that over-claims LISTEN/NOTIFY and contradicts §4.5's own correct doctrine that the tick alone is sufficient. NOTIFY is post-commit-only, undelivered to a reconnecting listener, folded when payloads are identical within a transaction, and dead under transaction-mode pooling. To avoid sleeping past a freed slot, adopt River's latch in shape: when a fetch is skipped for lack of capacity, set a `fetch_when_slots_available` flag; on any completion, if set, clear it and trigger the (debounced) fetch; and drain completions *while a fetch is in flight*. **The capacity wake must be level-triggered** ("re-evaluate capacity now"), never a counted credit.
5. **Precedence when both apply.** A run may legitimately be both exhausted on provider A and merely saturated on provider B. The presence of any merely-saturated candidate beats the presence of any exhausted one, and the run stays `queued`. Without this rule an implementer codes either order and gets 6-hour stalls half the time.
6. **Observability, because P5 otherwise trades a loud stall for a silent drip.** Emit one `run_events` row per deferral (`dispatch_deferred`, payload `{provider, reason}`) — shared with A7's `reason='global_cap'`. Add a "runs deferred on saturation" number to §9.2, render "deferred (provider busy)" distinctly from "queued" on the Runs page (§5.3 already establishes the precedent that a scary-looking-but-normal state needs explicit UI treatment), and **extend alert A2**, which today fires only on quota exhaustion and would stay silent while a run starves behind a peer stuck for the full 4-hour deadline.
7. **Protect doctrine #4's data.** Saturation on `claude` silently promotes `codex` to executor, and `run_attempts` records that as an ordinary codex attempt whose `matched_rule` says the rule chose claude. The outcome ledger — the one artifact doctrine #4 exists to produce — is contaminated by scheduling noise. Add `run_attempts.chain_position SMALLINT` and `selection_reason TEXT CHECK (… IN ('first_choice','fell_through_exhausted','fell_through_saturated','fell_through_auth','fell_through_provider_error'))`, and slice the §7.4 rollup on them.
8. **`max_concurrent_sessions` default for `providers.kind='local'` should be > 1.** A local inference server is not a single-account subscription CLI; inheriting the default 1 is an accident, not a decision — and it makes Ollama, the designated overflow valve, a bottleneck.

### A9 — Runner installation-token least privilege

> **Supersedes:** §6.6's "mints a GitHub App installation token (1 h TTL, one repo) per run"
> **Adds:** explicit `permissions: {contents: write}` on the mint call; §10.7 runbook row
> **Tests:** assert §6.6 states an explicit permission subset

**Highest-value security amendment in the set, and several attacks collapse without it.** §6.6 scopes the per-run token to one repo and one hour but never narrows its **permissions** below the §8.6 App ceiling (contents rw, pull_requests rw, issues rw, checks read, **actions read**, administration read). An implementer reading the document hands the agent the ceiling. Live consequences today:

- **`actions: read`** lets the agent fetch the previous attempt's workflow-run logs — where a failing assertion prints its own source line and expected/actual diff. Under A3 that is the holdout leaking, one draw per CI-red retry. (A3's no-retry rule and this token narrowing are the two halves of closing it.)
- **`issues: rw`** lets the agent edit its own task body — which the poller re-syncs into `backlog_items` and into the next run's `task.json`. **The agent has fed itself work**, which is exactly the v1 failure doctrine #5 exists to prevent, and it leaves no trace in Werft's own state. It also lets the agent remove its own `size:*` label, changing its routing chain and its quota ceiling.
- **`pull_requests: rw`** lets it edit the PR body the promotion summary is built from, and close/reopen PRs Werft tracks via `ux_runs_pr`.

**Change.** *The per-run installation token is minted with an explicit permission subset — `contents: write` and nothing else. The manager performs every PR, issue, and check operation with its own token; the runner's token can push commits and do nothing more.* Add a §11 test asserting the mint call passes an explicit `permissions` object, and a §10.7 row noting that an agent 403 on a GitHub call is expected, not an incident.

**Related, and §13 risk #3 must be rewritten.** §6.6 claims a prompt-injected run "can abuse the mounted session within that provider's API only (egress-limited)." That is false in a way that matters: the credential file is readable at a known path in a container that also holds a git token with `contents: write` and an allowlisted route to github.com. `cp /run/secrets/<cred> ./x && git add -A && git commit && git push` exfiltrates the whole-account provider session using only sanctioned mechanisms — no escape, no policy violation, no proxy alarm. §10.2's compensating controls catch known token *formats* and will not catch an opaque session blob. **Containment is rotation and detection, not prevention** — say so. Cheap partial control: the adapter refuses to push a commit whose diff contains bytes matching the mounted credential file (exact match — it has the file).

---

## 5. The paths nobody designed

### A10 — Consent is revocable

> **Supersedes:** — (defines `backlog_items.is_eligible`, which exists in §4.3 and appears in no prose)
> **Adds:** `runs.issue_snapshot JSONB`, `runs.issue_drifted BOOLEAN`; `parked_reason='consent_withdrawn'`
> **Tests:** assert `is_eligible` has stated semantics

Doctrine #5 implements only the accelerator. Nothing anywhere says what happens when the operator **removes** `werft:ready` from an issue whose run is `running`, closes the issue, edits the body after `task.json` was written, or transfers the issue. *Consent that cannot be withdrawn is not consent.*

- **`is_eligible`** means "the issue currently carries `werft:ready` and is open." The poller sets it false on label removal, close, lock, or 404. Rows are **never** deleted, so the ledger FK holds forever.
- **On `is_eligible → false`:** `queued`/`blocked_quota` → cancel; `claimed`/`running` → cancel and stop the container; **`awaiting_ci`/`merging` → park with `consent_withdrawn`**, not cancel — the work exists and the human should decide, and cancelling here races the merge (A11).
- **Snapshot every gate input.** `runs.issue_snapshot` freezes title/body/labels/size/`github_updated_at` at dispatch — it is exactly what went into `task.json`, so this stores an existing value. **General rule: any value used in a gate decision is a snapshot on `runs` written at dispatch, never re-read from a GitHub-mutable mirror at decision time.**
- **Drift is disclosed, never automatic.** `issue_drifted` is set when the live `github_updated_at` passes the snapshot, and surfaces in A15's promotion body. A run whose specification changed mid-flight is precisely a run the single human gate must look at — today the diff answers a question the operator no longer asked, with no indicator.

### A11 — GitHub-side effects of run transitions

> **Supersedes:** §8.1's branch table (the "human may push fixups to a parked run" cell) **Adds:** §8.7 table; `runs.branch_head_sha`; weekly PR-close sweep **Tests:** assert §8.7 exists with one row per terminal/parked edge

§4.2 specifies 30 legal edges with meticulous care about the database and says nothing about the other side. Three concrete gaps:

1. **`merging → canceled` is legal and `canceled` is terminal.** The merge call may already be in flight, so the run reads canceled while the PR merges — and `ux_runs_one_active_per_item` then frees the issue for a second run against work that already landed. Define: cancel from `merging` attempts the revoke first; if GitHub reports already-merged, the run goes to **`merged`**, not `canceled`.
2. **Parked runs leak open PRs forever.** §8.1 auto-deletes only *merged* heads. After months of unattended operation the repos accumulate dozens of open agent PRs — noise, and a standing invitation for someone to merge one by hand outside the gate. Add a weekly sweep (same timer family as the mirror repack) closing PRs for runs parked beyond the log-retention window.
3. **The force-reset destroys human work.** §8.1 says the dispatcher **force-resets `werft/run-<id>` to current `unattended` HEAD on every dispatch attempt**, and the same table says a human **may push fixups to a parked run**. Compose them: the operator pushes a fix, clicks Retry (`parked → queued`), the dispatcher force-resets, **and his work is gone with no warning and no recovery path.** Fix: record `runs.branch_head_sha` at each push; before force-resetting, compare; if the head moved, **refuse to reset** and dispatch onto a fresh `werft/run-<id>-a<n>` branch, noting the human commits in the PR body. *A human's commit is never destroyed by automation.*

The table is executed by the reconciliation sweep — idempotent and crash-safe, exactly like PR creation — not inline in the handler.

### A12 — Dependency circuit breaker with clock suspension

> **Supersedes:** — **Adds:** `manager_state.degraded_dependency`, `degraded_since`, `degraded_until`; alert A11; §10.7 row; §11 test **Tests:** none

**Walk the shipped spec through a six-hour GitHub outage.** Runs in `awaiting_ci` hit the 6 h CI-wait timeout and park. Runs in `running` fail their push (exit 4 → `infra_failure`), retry with backoff capped at 30 minutes, burn `max_attempts` in about an hour, and park. The dispatcher keeps claiming, minting tokens against a dead API, and **consuming real subscription quota on runs whose pushes cannot land** — quota reserved pessimistically that will not free until the window rolls. A3 fires once and sits under a 1-hour cooldown. At hour six the operator returns to a shredded queue with no single signal saying "GitHub was down."

Werft has a precise concept that *the agent must not be charged for GitHub's latency* — footnote ², the 4 h/6 h split, the whole reason CI-wait is a separate timeout — but **no concept of suspending those clocks**, and no concept of a dependency being down as distinct from a run failing. The machinery already exists; nothing is wired to it. This is the failure mode with the highest probability of actually occurring.

**Trip condition, mechanical and transport-level only, never inferred from a run outcome:** five consecutive GitHub API failures that are connection errors, 5xx, or rate-limit-exhausted, across the poller and merge handler, within five minutes.

**While tripped:** the dispatcher stops claiming (reuse the drain path verbatim); the CI-wait and hard-deadline sweeps are **suspended**, and on recovery each affected run's clocks are extended by the outage duration — the same principle footnote ² already asserts, applied to the case where the latency is infinite; `infra_failure` outcomes attributable to the tripped dependency **do not increment `attempt_count`**; per-run alerts are suppressed in favour of one **A11 dependency degraded**, firing on trip and again on recovery with duration and held-run count. Reset on the first successful call after a 60 s probe. The same breaker, different key, covers the Docker daemon and each provider API host.

**§11 test:** point the poller at a blackholed host; assert the breaker trips, **zero runs park**, and deadlines are extended on recovery.

### A13 — Offboarding is a state, and the current FKs are a data-loss trap

> **Supersedes:** `runs.project_id … ON DELETE CASCADE` and `promotions.project_id … ON DELETE CASCADE`
> **Adds:** `projects.archived_at`, `archived_reason`; `werftctl offboard`; `parked_reason='project_archived'`; YAGNI line
> **Tests:** assert both FKs are `ON DELETE RESTRICT`

Nothing in 806 lines describes removing a project, and the schema punishes the obvious attempt. The two CASCADE clauses sit in **direct contradiction** with §4.6's "`runs`/`run_attempts`/`promotions` never pruned (small rows, permanent ledger)": a single `DELETE FROM projects` is specified to destroy the permanent ledger. It does not even fail cleanly — `quota_ledger.run_id` and `promotion_runs.run_id` have no ON DELETE clause, so the cascade hits NO ACTION and the transaction errors halfway, at the psql prompt, on a production database, with no runbook.

Change both to **`ON DELETE RESTRICT`** — resolving the contradiction in the direction §4.6 already declares — and add "project hard-delete — never; archival only" to §12. `werftctl offboard` is an ordered idempotent flow mirroring onboard: pause → cancel queued, park in-flight as `project_archived` → **require the operator to resolve the parked set** (archival cannot silently strand work) → stamp `archived_at` → print but deliberately **do not execute** the GitHub-side teardown (the repo outlives Werft; destroying its branch protection on the way out is the worst possible parting act) → delete the mirror and run directories, the only reclaimable disk. Dispatcher, poller and metrics filter `archived_at IS NULL`; the ledger stays queryable forever, which is the entire point of calling it permanent.

### A14 — Operator-absence mode

> **Supersedes:** — **Adds:** an auto-drain policy on the reconciliation tick; a daily digest; Command Center banner **Tests:** none

**The largest operability hole in the shipped spec, and every amendment that adds a gate makes it deeper.** Werft's terminal outcome for automation is `parked`, every parked run demands a human, and A1 fires with **no cooldown by design**. Nothing anywhere answers "the operator is away for fourteen days." What actually happens: alerts accumulate until the topic is muted (the system is now unattended in the bad sense); retries consume subscription quota on work nobody will look at; `unattended` accumulates past the divergence banner; a dirty sync-back stalls silently; 20 MB run logs plus mirrors push disk toward the watchdog thresholds; an expired provider session sends every run down the chain to Ollama at full CI cost.

It is also the cheapest thing here to build, because every mechanism already exists. On the reconciliation tick: if parked runs for a project ≥ K, **or** no operator API mutation for D days with ≥ N runs parked, set `projects.is_paused = true` and flip `manager_state.accepting_new_runs = false`. Both columns exist; both are already respected; undrain is already a `werftctl` verb. Fire **exactly one** alert on entry, suppress individual A1s while drained, and collapse them into a daily digest. Zero new tables, zero new state-machine edges, zero new dashboard mutations.

---

## 6. The one human gate

### A15 — Risk-ordered promotion body, and freeze the head it describes

> **Supersedes:** §8.5's "structured, generated body (issue + PR + provider + duration per included run)"
> **Adds:** `werft/promo-<uuid>` as a fourth branch class in §8.1; a stable SQL ordering; `werftctl revert-run`
> **Tests:** assert the ordering is specified as a deterministic sort

**A latent bug in the shipped design, which the risk ordering would otherwise make worse.** `promotion_runs` is populated at promotion creation, but the PR head is `unattended`, **which keeps moving as more run PRs auto-merge while the promotion is open.** `ux_promotions_one_active` blocks a second promotion but does nothing to freeze the first. So the human's one gate reads a body describing runs 1–8 while merging runs 1–12. Today that is a latent inaccuracy; make the body load-bearing for his judgment and it becomes an active lie.

**Fix both with one change:** open every promotion PR from `werft/promo-<uuid>`, pinned at a fixed SHA — a fourth, explicitly ephemeral row in §8.1's branch model (lifetime: one promotion; writes: none; protection: none; auto-deleted on close). This makes `promotions.from_sha` mean what it says.

**Ordering** is a stable reproducible SQL sort living in a view with a unit test, not in template logic: `holdout absent or skipped` first — the only category where *no un-editable check ever ran* — then `touches_tests`, `touches_deps`, `issue_drifted`, `size='large'`, then `created_at`.

**Fail-closed rendering.** Any run whose flags could not be computed (file-list fetch failed, pagination truncated) renders **UNKNOWN and sorts first**, never false. A body that silently under-reports while looking complete is strictly worse than one that admits ignorance, because the human's single gate is calibrated on it. Print the exact head SHA the body describes, the matched paths for each flag (not just the boolean), and a one-line summary at the top.

**Give the gate an actuator below batch granularity.** Today it offers exactly two moves: promote everything including the risky run, or promote nothing. Add `werftctl revert-run <id>`, which opens a revert PR against `unattended` — squash-merge means one commit per run, so `git revert <merge_commit_sha>` is exact and `runs.merge_commit_sha` is already stored — through the normal strict-checks path.

**Throttle by back-pressure, not batch-splitting.** The Rosie lesson is that the reviewer is the scarce resource and unbounded automation floods them; Google needed a formal large-scale-change process for exactly this. But "promote the oldest N runs" is **rejected** (§R.4). The correct instrument is `projects.max_unpromoted_runs`: dispatch auto-pauses at the cap, exactly as §10.7 already auto-pauses on low disk. Automation throttles itself to reviewer capacity, with one column and no new topology.

### A16 — Dependency disclosure that a non-expert can act on

> **Supersedes:** — **Adds:** `runs.dep_changes JSONB`, `projects.dep_path_globs`; §6.7 registry decision; runner install hardening **Tests:** none

`touches_deps = true` tells a non-expert nothing — the same problem the v1 verdict identified. Store the **mechanical lockfile diff**: added/removed/version-changed package names from a per-ecosystem parser (pure text diff, no judgment). *"Adds `left-pad-utils` 0.0.3, new to this repo"* is reviewable; *"touches_deps: yes"* is not. Add a second free signal: flag packages appearing in this project's lockfile **for the first time in its history** — a SQL query over prior `dep_changes` rows, and where typosquats live.

**Scope the paths correctly.** "Manifest and lockfile" misses the highest-value target: **registry-redirection config** — `.npmrc`, `pip.conf`, `.netrc`, Cargo source replacement, `settings.xml`, `.yarnrc.yml` — none of which is a manifest, all of which point the installer at an attacker-controlled index without changing a single dependency name. Ship defaults keyed on `projects.language` with per-project override; **never** make correct configuration a prerequisite, or the flag reads false and the promotion body affirmatively tells the operator no run touched dependencies. A gate that lies confidently is worse than no gate.

**Specify where the changed-file list comes from** — nothing in the shipped document does, for `touches_tests` either. `GET /pulls/{n}/files`, paginated, computed at the transition into `awaiting_ci` **on the final head** (§8.1 force-resets and update-branch both move it), with an explicit cap and a fail-closed rule: if the list cannot be fetched or is truncated, set both flags true. Pessimism costs a human glance; optimism costs the gate.

**And force an unstated question into the open.** §6.7's allowlist names github, provider APIs, ntfy and telegram — **no npm, no PyPI, no crates.io.** Either runners cannot install dependencies at all (which makes most real coding tasks impossible and must be stated), or the allowlist is quietly wider than §6.7 claims and package registries are the single largest supply-chain door in the system. Decide it explicitly. Cheap hardening either way: `--ignore-scripts` and hash-pinned installs in the runner images' default environment.

**Honest framing, kept:** this is disclosure, not prevention — install scripts execute before any gate. Move that sentence into §13 as a named residual rather than leaving the honesty in a proposal.

---

## 7. Schema, enums, observability

### A17 — Prose-versus-schema reconciliation, made a standing invariant

> **Supersedes:** — **Adds:** the columns below; one Alembic migration; a structural test **Tests:** new test asserting every `identifier.column` in prose parses out of the §4.3 DDL, and vice versa

The original finding was two missing columns. A full pass finds more, and the fix should be the *pass*, not the patch.

**Referenced in prose, absent from §4.3:** `projects.oracle_attested_by/at` (§8.6 step 3), `projects.test_path_globs` (§8.6 step 4, §8.2a), `projects.github_installation_id` (§6.6 mints per-run installation tokens with no way to know the installation — load-bearing and absent), `projects.divergence_alert_days` (§8.3's "> 7 days unpromoted" has no column), `provider_accounts.credential_refreshed_at` (§6.6's "deliberate human act on an ntfy reminder" has no column, no alert row, and no timer — **the reminder does not exist**).

**Present in §4.3, absent from prose:** `backlog_items.is_eligible` — semantics undefined until A10.

**Name the glob matcher.** The prose defaults `tests/`, `test_*`, `*_test.*` are not valid patterns under any single matcher — `tests/` matches nothing under `fnmatch`, and `fnmatch`, `PurePath.match` and git pathspec disagree on `**` and on directory semantics. This is the archetypal invent-a-default trap and it is currently wide open.

**Closed-enum inventory.** `runs.parked_reason`, `run_attempts.outcome`, `promotions.status` and `run_events.event_type` are closed CHECK constraints. The amendment set adds values across all four: `oracle_unattested`, `test_integrity`, `holdout_red`, `check_missing`, `oracle_ambiguous`, `consent_withdrawn`, `project_archived` (parked_reason); `holdout_red` (outcome); `dispatch_deferred`, `holdout_observed` (event_type). **An incomplete CHECK is not a documentation nit — it is a 23514 raised inside the BEFORE UPDATE trigger's transaction, i.e. a state transition aborting at runtime in the merge path**, which is precisely the failure class §4.4 built the trigger to prevent, arriving through the front door. Ship them as **one** migration; add a test asserting every reason string appearing anywhere in either document is in the CHECK list.

### A18 — Alert table extension

> **Supersedes:** §9.5's table, presented as complete **Adds:** A8–A12 **Tests:** none

The amendment set introduces five silent failure modes and §9.5's premise — "unattended means *someone must be told*" — assumed stalls announce themselves through quota exhaustion. Three amendments break that premise.

| Alert | Trigger | Cooldown |
|---|---|---|
| **A8** holdout/required-check unavailable | expected check has not reported within N min | 30 min |
| **A9** protection drift (A4) | fingerprint mismatch; names the changed fields | none |
| **A10** required check never reported | `check_missing` park | none |
| **A11** dependency degraded (A12) | circuit breaker trip and recovery | none |
| **A12** **egress denied** | any `TCP_DENIED` line attributed to a live run | 1 h, rolled up per run |

**A12 is the highest-signal-per-line addition anywhere in this set.** Given a whitelist, a runner reaching for a host outside it is a direct mechanical indicator of prompt injection or attempted exfiltration — and today nothing in Werft looks at it.

**One more, covering the class rather than the causes** — a stall alert keyed on the invariant: *dispatchable work exists (project not paused, manager not drained, `next_attempt_at <= now()`) and zero runs have entered `claimed` for X minutes (default 30).* One SQL predicate, one cooldown row. It covers A7's slot leaks, A8's saturation loop, A3's holdout-reporter outage, and every future variant — where per-cause alerts would need three rows and still miss the fourth.

**Egress forensics, reframed.** The original proposal was to retain squid access logs. Two corrections: attribution is unsound as stated (squid's log keys on client IP, Docker recycles IPs on `runner_net`, and the manager's own traffic is interleaved), and **the signal is the denied lines, not the allowed ones** — given a whitelist, every allowed line is by construction unsurprising. So: record `runs.runner_ip` at container create, slice the window **at container teardown** into `/srv/werft/runs/<id>/egress.log` (the directory §9.1 already backs up, with the same 20 MB cap and 30 d/90 d retention — no new backup target, no rotation race), and make **A12 the headline** with the log as supporting evidence. State honestly that HTTPS yields `CONNECT host:port` and byte counts, not URLs — and that byte counts to an allowlisted host are themselves the exfiltration signal. **Retain for every run that reached `merging`/`merged` and every run flagged `touches_deps`, not only parked/infra-failed** — a successful exfiltration produces a clean, green, merged run, which is precisely what the parked-only rule discards. Carry §9.1's guard forward verbatim: **nothing behavioral ever keys off this log.**

### A19 — Meter the oracle's cost

> **Supersedes:** §2's "Actions-minutes cost at this scale is trivial" — a claim made before this amendment set existed
> **Adds:** `run_attempts.oracle_duration_seconds`; two §9.2 numbers; one alert
> **Tests:** none

A2 adds a base-commit collection pass, A3 adds a holdout job, A5 makes both required. In `strict_serialized`, every merge re-triggers the full check set on every other waiting run's updated head, so per-merged-run CI cost is roughly *check-set size × concurrent waiting runs* — plausibly 3–5× today's spend before any retry. Hosted minutes are free on public repos and metered on private ones. The failure mode for a solo operator is not an outage; it is a mid-month hard stop of all CI, which is a total stop of the merge path. Nothing in §9.2's fifteen numbers measures it.

The checks API already returns each check run's timestamps. Record duration on the existing `run_attempts` row; add "Actions minutes this month (est.)" and "**CI runs per merged run**" — the latter is the direct early warning that the strict-mode update-branch loop is thrashing. One alert at a configurable threshold, reusing `alert_state`.

**Bonus signal, nearly free.** With duration and A2's test counts recorded per run, an oracle that took six minutes and reported 400 tests for three months and now takes four seconds and reports 12 is **the `echo ok` failure mode arriving late, visible in two integers.** Alert when either drops below 0.5× its 30-day median. Park nothing — this is disclosure, like `touches_tests`, so it costs no autonomy. It is a regression detector, never a substitute for A1.

---

## R. Rejected, with reasons

The document's convention is that a rejected alternative is named with its reason, so it is not re-argued later. Each entry gets a **written revisit trigger**, per §12's own contract.

### R.1 — GitHub native auto-merge as the merge actuator

**Rejected.** The proposal was that Werft stop calling the merge API, arm auto-merge, and remain the branch-updater and observer — making it structurally unable to merge anything. Verification killed it on four independent grounds:

1. **It does not remove the branch-updater.** Auto-merge **never** updates the PR branch. Under strict mode, when the base moves, `mergeStateStatus` becomes `BEHIND` and the armed intent sits indefinitely — it does not expire and does not self-heal. GitHub's own documented answer to this gap is **merge queue**, not auto-merge. The busiest, most failure-prone loop stays exactly where it is.
2. **It does not remove the merge call.** Auto-merge can only be armed on a PR with at least one *unmet* requirement — arming an already-mergeable PR fails ("Pull request is in clean status"). So Werft must keep `PUT /pulls/{n}/merge` for the already-green case and choose between two paths at runtime. Two code paths where there was one.
3. **It creates durable state outside the database** — the anti-goal §4.5 and §8.6 name twice. A standing merge authorization on the PR object outlives every Werft run state. `POST /runs/{id}/cancel` is legal from every non-terminal state and does nothing to GitHub's flag, so a canceled run merges anyway, `ux_runs_one_active_per_item` frees the issue, and a second run starts against work that already landed. Same for a `ci_timeout` park whose check goes green at hour seven.
4. **Its stated benefit does not exist.** The window "Werft observed green, then state changed before its merge call" is already closed by strict mode — GitHub rejects the stale merge, and §4.2 footnote ⁵ exists precisely to loop it.

And it costs something real: **Werft's ability to refuse.** Actuating a merge is the dangerous act; withholding one is always safe. Today Werft can decline on policy GitHub cannot express — every assertion in A5. Under auto-merge the only policy language left is GitHub's required-check list. It also converts a designed, alerted outcome into a silent one: a CODEOWNERS-blocked PR currently returns 405 → `merge_blocked` → alert A1; armed, it just waits six hours and parks with the wrong reason.

Also noted: GraphQL-only (Werft pins httpx over REST), keyed by node ID, arming needs a repo setting whose absence fails at merge time rather than onboard time, invisible except via a webhook Werft has no listener for, and an unresolved March-2026 regression acknowledged by GitHub staff with no confirmed fix.

> **The one good idea inside it is salvaged and is worth more elsewhere:** *Werft interposes by making its gates named required status checks, not by holding the merge button.* That is A2, A3 and A5, and it needs no auto-merge at all.
> **Revisit trigger:** GitHub merge queue becomes available on personal-account repositories, at which point the whole `strict_serialized` design is reconsidered against it.

### R.2 — gVisor (runsc) as a per-project runner runtime

**Rejected, and the proposal was factually wrong about what it does.** It was offered as *additive* hardening. On the target platform it is **mutually exclusive** with SELinux confinement:

- `runsc` hard-fails at spec validation on any process SELinux label (`runsc/specutils/specutils.go`: `return fmt.Errorf("SELinux is not supported: %s", …)`), and dockerd sets that label on every container under enforcing mode. Every runsc container fails to start unless `--security-opt label=disable` is also passed.
- That workaround leaves dockerd with no MountLabel, and `opencontainers/selinux`'s `Relabel()` returns nil immediately on an empty label — so **every `:Z` bind-mount label in the document becomes a silent no-op**, with no error and no MCS category separation between projects.

So enabling it *replaces* an enforced kernel-resident MAC layer with a userspace kernel. Add: no dnf repository on Rocky 9 (manual install, weekly release cadence, a security-critical 100 MB binary on a manual patch treadmill); `--pids-limit` no longer bounds in-sandbox processes and a fork bomb panics the sentry rather than returning EAGAIN (open since 2021); memory accounts as `shmem` not `anon`, so any alerting keyed on `memory.stat` reads near-zero; Rust is absent from the vendor's per-release regression list; and network throughput is roughly −55%.

**And it aims at the wrong threat.** The dominant risk to whole-account provider credentials is not kernel escape — it is the runner's *legitimate* capabilities: `cp` the credential, `git push` it through the allowlisted host (A9). gVisor changes nothing about that. Finally, a default-off security control is the document's own definition of dead code ("an escape hatch with no consumer — cut in review") and forks both the §6.3 hardening-dict test and the §11 `container_t` rehearsal assertion.

> **Revisit trigger:** a demonstrated container escape from the pinned Docker CE version, or a second tenant on the VM — i.e. the moment the declared blast-radius boundary stops being accurate.

### R.3 — Repo-wide coverage-delta gating

**Rejected in favour of A2's test-count and skip-count deltas.** Coverage is the entire toil of the check and almost none of its signal. It requires per-language, per-project tooling config that grows linearly with projects the operator cannot read; it is nondeterministic under parallel or sharded runs, so a threshold tight enough to catch gaming produces false reds — **and every false red is a parked run and a no-cooldown page.** It cannot be collected from the base commit without building and running the base suite, doubling CI wall-clock on every PR. And the metric is wrong in both directions: it rises when you delete uncovered code and falls when you add well-covered code below the current average. Its config (`.coveragerc`, `omit=`, `collectCoverageFrom`) is agent-editable and outside `/.github/workflows/` — one `omit=` edit raises coverage while deleting code paths.

> **Revisit trigger:** a per-changed-file coverage floor, with config sourced from the fixed harness, once a project exists where the base build is fast enough to make it free.

### R.4 — "Promote the oldest N runs"

**Rejected: not expressible against the branch topology.** A promotion PR is `unattended → main` and carries the whole branch. Partial promotion requires cutting a snapshot ref, which permanently guarantees `unattended` is ahead of `main`, so the divergence banner fires as a standing condition and stops being a signal. Worse, when a partial promotion comes back red the operator holds a red mid-history point and must hand-bisect a batch — precisely the workload §12 refuses to build machinery for. **The lever is promotion *frequency*, not promotion *size*** (A15's back-pressure cap). The snapshot ref is still adopted — but for head-freezing, which is a correctness fix, not for capping.

### R.5 — LLM-assisted review of the promotion diff

**Rejected pre-emptively**, because it will be proposed the first time a promotion body is 40 runs long. It is the v1 gate returning in a new costume: a model's opinion placed in front of the one human gate, where deference to it is the documented failure. The evidence is also weaker than it looks — the strongest published judge result rests on twelve true positives and its own authors warn it likely does not transfer to real codebases; an independent evaluation across 54 hack categories reports 63% detection; and LLM-judge prompt injection is a named flaw class.

> **Revisit trigger:** none. This is an anti-goal, not a YAGNI item.

### R.6 — Holdout secrecy as a security control

**Rejected as a *control*; retained as a *property that degrades gracefully*.** See A3. The holdout necessarily executes against agent-authored code on a network-unrestricted hosted runner, so its confidentiality is not defensible and must not be load-bearing.

### R.7 — Self-hosted holdout execution on the Werft VM

**Rejected pre-emptively.** It is the obvious cost-saving move once A3 exists, and it re-imports the lateral-movement risk that §8.2 rejected self-hosted CI to avoid, plus a second execution engine.

### R.8 — Persistent agent memory / cross-run context (AIOS-style)

**Promoted out of the YAGNI ledger into the README anti-goals**, where changing it requires a deliberate doctrine edit. It is not a "not yet" — it is state outside the database by construction, and it breaks §8.1's guarantee that every attempt starts clean by making attempt N+1 depend on attempt N's *unverified* conclusions. It is also the exact leakage channel A3 depends on being closed.

### R.9 — Other ledger additions, each with a trigger

| Item | Revisit trigger |
|---|---|
| eBPF-driven runner observability | a runner escape is suspected and container-level evidence proved insufficient |
| AgentCgroup-style semantic resource control | the 2 vCPU / 4 GB / pids 256 envelope is demonstrated to be the binding throughput constraint |
| Kata / Firecracker isolation | the VM stops being the accepted blast-radius boundary |
| Zuul-style speculative/batched merge with bisection | > 10 merges/hour — two orders of magnitude above the stated scale. §8.2 already half-decides this ("bisection flows are not modeled because batching is not used"); cite it rather than re-deciding |
| **Werft authoring or mutating project CI workflow files** | never — §8.2 says "it never authors or edits project CI logic," and A2/A3 both push toward it. Write the boundary down or it erodes |
| **LLM-generated promotion or PR body text** | never — §8.4/§8.5 specify templated bodies, and A15's richer body is exactly where "just have a model summarize the batch" gets proposed |
| **Auto-promotion on any heuristic** (all-green, N days quiet, low risk score) | never — §8.5's "Werft may nudge; it never auto-promotes" is doctrine #2's actual enforcement |

---

## D. Doctrine and framing

### D1 — Retire the "agentic OS" tagline

> **Supersedes:** README line 3 **Adds:** a scope statement **Tests:** `assert.doesNotMatch(readme, /agentic OS/i)`

The label is a claim about breadth; the thesis is narrowness. The v1 verdict names the only honestly-viable survivor as *"a drastically narrower tool … only in repositories with strong existing test suites, behind a real executed gate."* "OS" invites a kernel, a memory manager, a scheduler abstraction — literally the items §R.8 and §R.9 reject. **D1 and the YAGNI ledger are one argument.**

Deletion alone is an incomplete edit; a tagline is structurally required and an empty slot gets refilled with something worse. Replace it with the scope statement that does double duty — retiring the breadth claim *and* stating the onboarding precondition A1 now enforces:

> **Unattended coding agents on repositories with an oracle proven to catch a wrong change. Nothing merges that an executed gate has not passed. One operator, one VM.**

Note the precedence subtlety: ARCHITECTURE.md states that README **governs**, so this document cannot amend its own governor. D1 is applied to README.md directly; this entry only records that it was done. While in the file, close verification finding **N4**: the architecture blurb still permits "self-hosted Woodpecker/Gitea, or GitHub Actions" while §2/§8.2 pin GitHub-hosted Actions and reject self-hosted outright.

### D2 — Every gate carries a removal trigger

> **Supersedes:** — **Adds:** a SQL view; one sentence of change discipline; a monthly review slot **Tests:** none

§12 governs what is never built. There is no symmetric discipline for what *has* been built and now costs more than it returns — and gates are exactly the components whose cost is invisible at design time and crushing at 22:00 on a Tuesday. **v1 accreted three engines by precisely this mechanism: each addition locally justified, nothing with a written exit.**

For a one-person system the decisive question about A2 and A3 is not "are they correct" but "when are they removed." Without an answer, the realistic outcome is not removal — it is that they get widened until they pass everything, keeping the operational cost and deleting the signal.

A plain SQL view over `runs.parked_reason` and `run_attempts.outcome` gives parks-per-reason-per-week. **Any gate whose reds are predominantly judged spurious over a rolling month is removed, not tuned**, with the removal recorded as an ADR under §11. Attach the review to the monthly cadence that already exists for the restore drill, so it costs one recurring calendar slot rather than a new habit.

---

## Appendix A′ — New tunable constants

| Constant | Default | Lives in | Amendment |
|---|---|---|---|
| `WORKER_COROUTINES` | 4 | env | A7 |
| `MAX_CONCURRENT_RUNS` (redefined: concurrent runner containers, DB-enforced) | 4 | env | A7 |
| `WERFT_SATURATION_DEFER_SECONDS` | 15 s | env | A8 |
| Protection-fingerprint verify poll | 5 min | env | A4 |
| Mutation floor | 0.60 | `projects.mutation_floor` | A1 |
| Mutation measurement cadence | weekly | scheduled workflow | A1 |
| Oracle attestation expiry | 90 d | `projects` | A1 |
| Holdout job timeout | 30 min | `projects` | A3 |
| Circuit-breaker trip / probe | 5 failures in 5 min / 60 s | env | A12 |
| Auto-drain thresholds (K parked, D days idle) | 6 / 5 d | env | A14 |
| `max_unpromoted_runs` | 25 | `projects` | A15 |
| Monthly Actions-minutes alert threshold | operator-set | env | A19 |
| Oracle-regression ratio | 0.5 × 30-day median | `projects` | A19 |

## Appendix B′ — Structural test additions

Every load-bearing claim in this repo is locked by `tests/architecture_spec.test.mjs`; these amendments follow the same rule.

1. A0's four legs stated verbatim.
2. `werft-integrity`, `werft-holdout` named as fixed check names; §8.2's "no repo/org secrets exposed to the oracle workflow" sentence still present and not contradicted.
3. CODEOWNERS file path is `.github/CODEOWNERS` and `/.github/` is owned wholesale.
4. Every `parked_reason` / `run_attempts.outcome` / `run_events.event_type` string appearing anywhere in either document is present in the §4.3 CHECK list.
5. Every `identifier.column` mentioned in prose parses out of the §4.3 DDL block, and vice versa — converting A17 from a one-time patch into a standing invariant.
6. `runs.project_id` and `promotions.project_id` are `ON DELETE RESTRICT`.
7. Appendix A carries `WORKER_COROUTINES` and `MAX_CONCURRENT_RUNS` as distinct rows.
8. `assert.doesNotMatch(readme, /agentic OS/i)` and the new scope statement present.
9. Every schema column and required-check name declared here also appears in ARCHITECTURE.md — **so the two documents cannot silently diverge while this file exists.**

---

## Ordering, if only some of this is built

Ranked by value per unit of work, and by what breaks without it:

1. **A6** (CODEOWNERS takeover) — an exploitable defect in the shipped spec, closed by moving one file.
2. **A9** (token least privilege) — one parameter on the mint call; several other attacks collapse with it.
3. **A4** (protection fingerprint) — one authenticated GET the manager is already permissioned for, guarding the enforcement of both doctrines #1 and #2.
4. **A7 + A8 together** — real bugs in shipped SQL that bite on day one. **They must land together**: A7 alone gives "zero rows" a third meaning and becomes a system-wide stall generator.
5. **A17** (schema reconciliation) — one migration, and the standing test that stops the drift recurring.
6. **A1** (executed discrimination gate) — the largest doctrine-#1 hole in the system, and the v1 verdict's own prescription.
7. **A12, A10, A11, A13, A14** — the undesigned paths. Cheap, and each is a guaranteed-to-occur scenario.
8. **A15's ordering and head-freeze** — the only artifact the human gate actually reads, currently able to describe a set it does not merge.
9. **A2**, then **A3** — the gates. A3 is the most expensive item here and the one most likely to be quietly switched off; it should not be attempted before A1 exists to prove a candidate holdout can fail.
10. **D1, D2, R.\*** — free, and D2 decides whether A2 and A3 are still alive in six months.
