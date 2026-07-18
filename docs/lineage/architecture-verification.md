# Werft Architecture Verification Report

**Subject:** `ARCHITECTURE.md`  
**Overall readiness verdict:** **buildable with fixes**

---

## Report header — sources examined

| Source | Path | Status / version line | Role |
|---|---|---|---|
| Architecture (primary) | `ARCHITECTURE.md` | **Groundwork specification, v1.1 (2026-07-18)** | Buildable blueprint under test |
| Doctrine | `README.md` § "Doctrine (founding decisions)" | Five numbered decisions; Status: "Groundwork specified" | Governs when conflict with architecture |
| Lineage post-mortem | `docs/lineage/v1-verdict.md` | **Archived — 2026-06-05**; decision: stop development | Failure modes the redesign must address |
| Other specs | `docs/superpowers/specs/` | **Empty** (no files) | No competing architecture fragments |
| Repo tree | groundwork-only | No `manager/`, `runners/`, or `deploy/` trees yet | Document-only analysis |

**Note:** `README.md` Status still says ARCHITECTURE is **v1.0**; the architecture document is **v1.1**. Doctrine content itself is unchanged by that version label drift (finding N4 below).

**Analysis date:** 2026-07-18  
**Method:** full-document cross-read of the three sources above; edge inventory of §4.2; contract walk of the seven load-bearing claims; mapping of v1-verdict / anti-goal items to named mitigations.

**Scope:** verification only — no edits to `ARCHITECTURE.md` or `README.md`.

---

## Executive summary

Werft's planned architecture is a coherent, doctrine-aligned redesign of Claude Agent Station. It correctly inverts v1's failure modes: the merge path is an **executed** GitHub Actions oracle (no LLM judgment), intake is **human-labeled only**, routing is a **static YAML table** with outcome recording and no learned loop, providers are **process-layer subscription CLIs** in ephemeral containers, and blast radius is **branch topology** (`unattended` vs `main`) with human promotion.

Prior external-review fixes claimed in the v1.1 header — oracle mutability surfacing (§8.2/§13#8), Docker-wait vs long-lived-coroutine redesign (§6.1), installation-token re-mint (§6.6), advisory-lock claim transaction (§7.2), manager egress topology (§10.1) — are present and largely consistent. Two residual document defects remain load-bearing enough to block a pure "buildable as-spec" verdict:

1. **§6.4 still names `docker wait` as completion authority** while §6.1 forbids per-run blocking wait and mandates events + inspect (the exact contradiction v1.1 claims to have fixed).
2. **§4.2 table cells `awaiting_ci → failed` and `merging → failed` are marked footnote ²** (agent hard-deadline), but footnote ² explicitly excludes those states and routes CI timeout through park edges ⁴/⁶.

These are **major** documentation inconsistencies an implementer can mis-read into the wrong runtime. They do not invalidate the overall design. No **blocker** was found that would make the system doctrine-violating or unbuildable once the two majors are corrected.

**Overall readiness:** **buildable with fixes** (highest open severity = major; no blockers).

---

## Doctrine matrix (README #1–#5 vs ARCHITECTURE.md)

| # | Doctrine (README) | Verdict | Architecture mechanism(s) | One-sentence rationale |
|---|---|---|---|---|
| **1** | Verification is executed, never judged. Only green CI merges; no LLM verdict; red/untestable parks. | **PASS** | §1 steps 4–5; §6.4 two-tier completion; §8.2 CI oracle + strict merge path; §5.3 error taxonomy (no LLM class); §12 YAGNI bans LLM merge/verdict logic | Merge authority is GitHub Actions green/red on the post-update branch head; retries are fresh dispatches; parking is the automation terminal for red; "No LLM opinion exists anywhere in this path" is structural, not aspirational. Residual: oracle quality / agent-editable tests — see residual risks (accepted #8), not a doctrine miss. |
| **2** | Blast radius contained by branch topology. Agents land on `unattended`; promote `unattended → main` is human + re-runs pipeline. | **PASS** | §1 step 6; §8.1 branch model; §8.5 promotion; §8.4 sync-back; `promotions` schema §4.3 | Run PRs target `unattended` only; promotion is an explicit dashboard action opening a batch PR that re-runs the same oracle against `main`; Ken merges; sync-back prevents silent drift. |
| **3** | Providers are subscription CLIs at the process layer; thin adapters; manager meters plan quotas; Ollama free overflow; no gateway / per-token core path. | **PASS** | §6 (lifecycle, images, adapters); §6.4/§6.5; §7.2–§7.3 quota windows/sessions not tokens; §7.1 default chain ends in `ollama`; observational tokens display-only | Four CLI adapters in ephemeral containers; quota is rolling windows + weekly run-count + sessions; token counts never feed decisions; Ollama is the documented overflow valve. |
| **4** | Routing is static YAML + outcome recording; no learned router unless data later proves the table wrong. | **PASS** | §7.1 `routing.yaml`; §7.2 pure `decide_dispatch`; §7.4 `run_attempts` + import-linter forbids routing↔analytics import; §4.3 `run_attempts` schema | First-match static rules; day-one outcome ledger; mechanical ban on feedback into the router package. |
| **5** | Backlog is human-fed only; manager works only issues you label. | **PASS** | §1 step 1 ("only intake path"); §4.3 `runs.backlog_item_id NOT NULL` + `backlog_items` mirror of `werft:ready`; §8.6 labels; §12 bans agent-generated backlog | Intake is poll-of-label only; DB enforces every run traces to a human-labeled issue; no analyst/self-file path exists in the design. |

**Matrix completeness:** all five doctrine rows present with named sections and pass/fail/partial. **Result: 5/5 PASS.** No doctrine failure or partial.

---

## Consistency findings — run state machine (§4.2)

### States (10)

`queued`, `claimed`, `running`, `awaiting_ci`, `merging`, `blocked_quota`, `failed`, `parked`, `merged`, `canceled`

**Terminal:** `merged`, `canceled` only. `parked` is requeueable (`parked → queued` ⁹) — matches prose and v1 contradiction fix. Consistent.

### Legal-edge inventory (every ✓ cell)

| Edge | Footnote / mark | Justifying prose path | Status |
|---|---|---|---|
| `queued → claimed` | — | §1 step 2; §7.2 claim+quota transaction | OK |
| `queued → blocked_quota` | ᵃ | §7.2/§7.3 all providers exhausted at dispatch (never claimed without quota) | OK |
| `queued → parked` | ᵇ | §5.3 PermanentError pre-attempt | OK |
| `queued → canceled` | — | §4.2 diagram; §5.4 cancel from every non-terminal | OK |
| `claimed → queued` | ¹ | lease expired before container start | OK |
| `claimed → running` | — | §1 step 2–3 container launch | OK |
| `claimed → failed` | ² | hard-deadline on agent-execution states | OK (deadline applies to `claimed`) |
| `claimed → canceled` | — | human cancel | OK |
| `running → awaiting_ci` | — | §1 steps 3–4 success path → open PR → CI | OK |
| `running → failed` | — (text: lease vanish, attempt end) | §4.2 ¹ running lease; attempt failure §5.3; hard-deadline prose for `running` | OK |
| `running → canceled` | — | human cancel | OK |
| `awaiting_ci → queued` | ³ | §1 step 5 CI red + budget left → fresh dispatch | OK |
| `awaiting_ci → merging` | — | §1 step 5 / §8.2 CI green | OK |
| `awaiting_ci → failed` | **²** | **See finding M1** — footnote ² excludes `awaiting_ci` | **MAJOR** |
| `awaiting_ci → parked` | ⁴ | CI red budget spent; CI timeout (`ci_timeout`) via ⁴ | OK |
| `awaiting_ci → canceled` | — | human cancel | OK |
| `merging → awaiting_ci` | ⁵ | §8.2 base moved; re-update; fresh green required | OK |
| `merging → failed` | **²** | **See finding M1** — footnote ² excludes `merging` | **MAJOR** |
| `merging → parked` | ⁶ | merge conflict; also CI-wait timeout path per ² text; `merge_blocked` protection refuse | OK |
| `merging → merged` | — | §1 step 5 squash-merge success | OK |
| `merging → canceled` | — | human cancel | OK |
| `blocked_quota → queued` | — | §7.3 wake at `min(exhausted_until)` / window edge | OK |
| `blocked_quota → canceled` | — | human cancel | OK |
| `failed → queued` | ⁷ | retry with backoff (`next_attempt_at`); chain fallthrough path | OK |
| `failed → blocked_quota` | ᵃ | mid-run exhaustion ends attempt, all remaining providers exhausted | OK |
| `failed → parked` | ⁸ | chain-cycle budget exhausted; also PermanentError "via failed → parked" | OK (⁸ text is budget-only; PermanentError reuses edge — **minor** footnote under-specificity) |
| `failed → canceled` | — | human cancel | OK |
| `parked → queued` | ⁹ | human requeue / `POST …/retry` | OK |
| `parked → canceled` | — | human cancel | OK |

**Terminal rows:** `merged` and `canceled` correctly have no outgoing transitions.

### Prose flows → table edges

| Flow | Expected transition | In table? |
|---|---|---|
| §1.1 intake | insert `queued` (create, not transition) | N/A OK |
| §1.2 claim + quota | `queued → claimed` or `queued → blocked_quota` / `queued → parked` | Yes |
| §1.3 runner completion → PR | `claimed → running → awaiting_ci` | Yes |
| §1.5 green merge | `awaiting_ci → merging → merged` | Yes |
| §1.5 red + budget | `awaiting_ci → queued` ³ | Yes |
| §1.5 red + spent / park | `awaiting_ci → parked` ⁴ | Yes |
| §1.5 merge conflict | `merging → parked` ⁶ | Yes |
| §5.3 TransientError retry | ends in `failed → queued` ⁷ | Yes |
| §5.3 ProviderError fallthrough | via `failed → queued` then re-claim | Yes (implicit; see minor N1) |
| §5.3 QuotaExhaustedError | `→ blocked_quota` from queued or failed | Yes |
| §5.3 GitConflictError | `→ parked` (merging/branch-update) | Yes |
| §5.3 PermanentError | `queued → parked` or `* → failed → parked` | Yes |
| §7.2 claim without reservation | never `claimed` without quota | Consistent |
| §7.3 all-exhausted wake | `blocked_quota → queued` | Yes |
| §8.2 rebase/update loop | `merging → awaiting_ci` ⁵ | Yes |
| §8.2 merge refused by protection | park `merge_blocked` | Yes (`merging → parked`) |
| §8.4 sync-back | **not a run status** (separate PR path + A7) | Correctly outside run SM |
| §8.5 promote | **`promotions` table**, not `runs.status` | Correctly outside run SM |
| Human cancel any non-terminal | `* → canceled` | Yes for all 8 non-terminal sources |
| Human retry | `parked → queued` | Yes |

**No orphan prose transition** that invents an illegal from→to pair was found.

**Orphan / mislabeled table edges:** `awaiting_ci → failed` and `merging → failed` exist and are *plausible* for PermanentError / infra during CI/merge, but they are **wrongly annotated with footnote ²**, which contradicts itself. Not orphan edges — **mislabeled load-bearing annotations** (M1).

### Diagram vs table (minor)

ASCII diagram groups claimed's down-arrow as "(lease/deadline)" landing near `parked`/`failed`. Table correctly splits: lease-before-start → `queued` ¹; deadline → `failed` ². **Minor diagram imprecision** (N2), not a table bug.

---

## Consistency findings — cross-subsystem contracts

| # | Contract | Verdict | Citations | Notes |
|---|---|---|---|---|
| 1 | **One-writer / DB-blind runners** | **Consistent** | §4.1; §5.4; §6.3 filesystem contract; heartbeats written by manager observation only (§4.3) | Runners: `task.json` in, `result.json`+log+exit out. Dashboard/werftctl/watchdog talk only to API. |
| 2 | **Claim + quota reservation atomicity** | **Consistent** | §1 step 2; §7.2 single transaction with `pg_advisory_xact_lock`, guarded INSERT, CAS claim; one candidate per txn; rollback on zero rows | READ COMMITTED race called out and fixed; cross-account deadlock avoided by single-account-per-txn. |
| 3 | **Exit-code vs result.json authority** | **Major inconsistency (stale phrase)** | §6.4 two-tier table + "exit ≠ 0 discards result.json"; §1 step 3; **but** §6.4 last sentence still says completion authority is `docker wait` + `result.json` while §6.1 uses events + `inspect` | Semantic two-tier design is sound. Residual "docker wait" wording reintroduces the fixed bug if followed literally → **M2**. |
| 4 | **Docker events (not blocking wait) vs no-long-lived-coroutine** | **Consistent** (with M2 residual wording) | §6.1 events stream as sanctioned long-lived task (same class as LISTEN); §5.1 TaskGroup includes Docker events reader; advance() handlers short-lived §5.2 | Structural fix matches §5.1/§5.2. Only the §6.4 phrase lags. |
| 5 | **Strict CI merge guarantee** | **Consistent** | §8.2 `strict_serialized`: protection "up to date", update-branch, green on updated head, serialize merges; base-move → `merging → awaiting_ci` ⁵; optional `merge_queue` batch size 1 | "Merged result" is GitHub-enforced, not SHA heuristics. |
| 6 | **Git-token re-mint vs 90 min ceiling** | **Consistent** | §6.6 1 h installation token; 90 min adapter ceiling; re-mint/rewrite host file at 45 min; `GIT_ASKPASS` reads file per op; Appendix A | External-review TTL bug is addressed. |
| 7 | **Compose network reachability (manager egress + runners)** | **Consistent** | §10.1 dual-home egress-proxy on `runner_net` + `mgr_egress` + `internet`; manager `HTTPS_PROXY`; runners `runner_net` internal; ollama on `runner_net`; §6.7 allowlist | Draft "manager has no route to GitHub" is fixed; uniform egress discipline applied. |

### Prior-review fix re-check

| Claimed fix | Present? | Residual? |
|---|---|---|
| Oracle-mutability §8.2/§13#8 | Yes — `touches_tests`, promotion flagging, coverage-floor encouragement, optional CODEOWNERS, accepted risk #8 | Residual risk accepted honestly |
| Docker-wait §6.1 | Yes — events + inspect design | **Stale "docker wait" in §6.4 (M2)** |
| Installation-token TTL §6.6 | Yes — 45 min re-mint | None |
| Advisory-lock claim txn §7.2 | Yes — `pg_advisory_xact_lock` + one candidate/txn | None |
| Manager egress topology §10.1 | Yes — `mgr_egress` + proxy env | None |

---

## Findings register (severity)

### Major

**M1 — Footnote ² misapplied to `awaiting_ci → failed` and `merging → failed`**  
§4.2 table marks those cells with ², but footnote ² states the hard-deadline sweep bounds **only** `claimed`/`running`, and that `awaiting_ci`/`merging` use `WERFT_CI_WAIT_TIMEOUT` which **parks** via ⁴/⁶ with `ci_timeout`.  
- If the edges are intentional for PermanentError/infra during CI/merge, renumber/relabel footnotes.  
- If the edges were meant to be deadline/CI-timeout, they contradict the park path and should be removed.  
**Impact:** implementer may force-fail CI-wait runs into `failed` (burning retry semantics) instead of parking with `ci_timeout`.

**M2 — §6.4 completion authority still says `docker wait`**  
Line-level: *"completion authority is always `docker wait` + `result.json`"*.  
§6.1 (and the v1.1 header) replaced per-run blocking `containers/{id}/wait` with Docker **events** + **inspect**.  
**Impact:** same class of bug the external review fixed; a faithful implementer of §6.4 alone reintroduces worker-pool starvation / long-lived-per-run wait.

### Minor

**N1 — Provider fallthrough path is implicit**  
§5.3 says fallthrough does not consume chain-cycle budget and "steps to the next entry of `provider_chain`". Legal path is only via `running → failed → queued → claimed` (no direct `running → claimed`). Works, but attempt/outcome rows and dashboard flash of `failed` during healthy fallthrough should be specified so implementers do not invent illegal edges.

**N2 — ASCII diagram lease/deadline conflation**  
Diagram labels claimed down-arrow "(lease/deadline)" near parked; table correctly splits lease→queued vs deadline→failed.

**N3 — Footnote ⁸ only mentions budget; PermanentError reuses edge**  
Prose: every other PermanentError parks via `failed → parked`. Edge exists; footnote text is incomplete.

**N4 — README version / CI-oracle breadth drift**  
- README Status cites ARCHITECTURE **v1.0**; document is **v1.1**.  
- README architecture blurb still allows "self-hosted Woodpecker/Gitea Actions, or GitHub Actions"; ARCHITECTURE §2/§8.2 pins **GitHub-hosted Actions** and rejects self-hosted on the manager VM. Doctrine still holds either way; blueprint is narrower.

### Blocker

**None.**

---

## v1-verdict / anti-goal → mitigation map

### Core v1-verdict failure modes

| Failure mode (v1-verdict / lessons) | Mitigation in ARCHITECTURE.md | Residual? |
|---|---|---|
| **LLM judgment gate** | §1 step 5 explicit ban; §8.2 executed GHA oracle; §5.3 no judgment error class; §12 bans LLM merge/conflict/verdict logic | None structural — residual is oracle *strength*, not LLM return |
| **Non-executed verification** | §8.2 GitHub Actions hosted runners execute agent-authored code off-VM; branch protection strict up-to-date; green required before squash-merge | Oracle can still be a weak workflow — see next rows |
| **Reward-hacking / agent-editable tests (oracle)** | §8.2 layered: (a) `runs.touches_tests`; (b) promotion PR flags test-touching runs; (c) encouraged coverage-floor in `werft-oracle.yml`; (d) optional CODEOWNERS on tests; doctrine #2 containment to `unattended` | **Yes — Accepted risk #8 (§13).** Not eliminated. |
| **Oracle strength is human-attested only** | §8.6 step 3: `werftctl onboard` human checklist + `oracle_attested_by/at`; unattested projects dashboard-warn | **Yes — inherent.** System cannot mechanically prove a workflow is a real suite. |
| **Multi-engine accretion** | §3 import-linter DAG; §5.1 one uvicorn worker / one engine; §4.1 one writer; §12 | Residual: discipline + CI gate |
| **Backlog self-generation** | Doctrine #5; §1 only `werft:ready` intake; `backlog_item_id NOT NULL`; §12 | None |
| **Branch drift** (v1: 532 commits) | §8.4 CI-gated sync-back; A7 alert; §8.3 divergence banner; never rebase long-lived branches | Human must resolve dirty sync-backs (alerted) |
| **State outside DB** | §4.1 runs row is queue+state; `ux_runs_one_active_per_item`; §8.6 "no label is ever a lock" | None structural |
| **Keep-alive / long-lived coroutine tax** | §5.2 short-lived `advance()`; waits in DB timestamps; §6.1 events not per-run wait | Residual only if M2 reintroduced at implement time |

### README anti-goals

| Anti-goal | Coverage |
|---|---|
| No second execution engine | §3, §5.1, §12, import-linter |
| No LLM-judgment gates in merge path | §1, §8.2, §12 |
| No agent-initiated replatforming of Werft substrate | §10.5 Werft not onboarded to itself; §12 |
| No state outside the database | §4.1, §4.5, §8.6 |
| Dashboard serves the loop | §9.4 five-page hard cap; six UI mutations only |

---

## Residual risks (architecture-accepted + analysis-noted)

From §13 (eyes open), all still valid:

1. Single VM / Postgres / manager SPOF  
2. Provider CLI churn (Kimi weakest adapter)  
3. Whole-account provider credentials in runners  
4. Polling latency + GitHub dependence  
5. Day-1 quota estimates until first rejection  
6. Egress allowlist manual toil  
7. GitHub App key fleet-wide (no admin:write)  
8. **Agent-editable test suite / reward-hacking vector** — layered mitigations, not immunity  

**Analysis adds (documentation risks, not new system risks):** M1, M2 must be fixed before or during first implementation of the runner lifecycle and CI-wait reaper so implementers do not re-encode the bugs.

**Oracle strength human-attested only** is correctly treated as an operational limit, not a silent assumption.

---

## Other architecture / spec fragments

Under `docs/`:

- `docs/lineage/v1-verdict.md` — historical post-mortem only; does not define target architecture. No conflict.
- `docs/superpowers/specs/` — **empty directory**.

No additional architecture fragments exist that could conflict with `ARCHITECTURE.md`. README high-level blurb is broader on CI hosting (N4) but does not redefine the buildable blueprint.

---

## Overall readiness verdict

| Rule | Application |
|---|---|
| Any **blocker** → not ready | **0 blockers** |
| Only **majors** (or majors + minors) → buildable with fixes | **2 majors (M1, M2)** + several minors |
| Else → buildable as-spec | Not applicable |

### **Verdict: buildable with fixes**

The architecture is doctrine-complete (5/5 PASS), v1 failure modes are mapped to named mitigations or honest residual risks (including reward-hacking and human-only oracle attestation), and the seven cross-subsystem contracts are consistent except for the two major documentation defects that undoing prior review work if followed literally.

**Minimum fixes before treating the doc as implementable as-spec:**

1. **M2:** Change §6.4 completion-authority sentence from `docker wait` to the §6.1 mechanism (`die` event / reconciliation inspect + `result.json`).  
2. **M1:** Either remove `awaiting_ci → failed` / `merging → failed` if unused, or re-footnote them for PermanentError/infra (and keep CI timeout exclusively on ⁴/⁶ park paths).

Optional polish: N1–N4 (fallthrough path explicitness, diagram, footnote ⁸, README v1.1 + CI hosting alignment).

---

## Appendix — Edge count summary

- Non-terminal states with outbound rows: 8  
- Distinct legal ✓ edges inventoried: 30  
- Edges with major annotation defects: 2 (`awaiting_ci → failed`, `merging → failed`)  
- Prose flows checked against table: 18 — 0 illegal from→to pairs  
- Doctrine rows: 5/5 PASS  
- Contracts: 6 consistent, 1 major residual wording (exit/wait authority)  
- v1 failure modes mapped: 9/9 (all mitigated or residual-accepted)

---

## Durable proof (repo)

| Artifact | Location |
|---|---|
| This report | `docs/lineage/architecture-verification.md` |
| Structural tests (read-only against ARCHITECTURE.md) | `tests/architecture_spec.test.mjs` |

Run tests: `node --test tests/architecture_spec.test.mjs`

*Verification only — `ARCHITECTURE.md` was not modified.*
