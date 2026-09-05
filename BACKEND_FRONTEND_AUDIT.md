# Backend / frontend capability audit

## Implementation follow-up — 2026-09-05

The seven existing-API gaps below are now addressed on `codex/agentic-os-frontend`:

- Tasks, Review, project filters, and command search query the server with pagination; review totals no longer depend on the first 200 tasks. Multi-status and literal text search are supported by `GET /runs`.
- Session numbering is separate from the failure budget. Mutation responses immediately update task state, and bounded backend error details reach the operator.
- Project settings expose the existing manual lifecycle flip and its branch-protection consequences.
- Evidence includes structured results/usage, grouped files, collection timestamps, hashes, authenticated downloads, and bounded inert text previews.
- Every labelled VM container can open its task directly by ID. Container image and quota observation/source diagnostics are available in drilldowns.

Five authenticated read routes expose previously internal information: per-run runtime and incremental retained logs, paginated cross-run events, project lifecycle events, and safe dispatch/configuration metadata. Configuration presence and schema validation are explicitly distinct from connectivity verification. Raw logs remain raw output, not a reconstructed tool-action stream. Session output is bounded and cursor-based, with reset detection across file replacement and execution attempts.

The inspector tab freeze was a reactive session-output loop. Polling now keeps cursor changes outside effect dependencies, serializes visibility wakeups, cancels on disposal, and clears old output when switching tasks. Preview mode renders one static sample.

Validation includes real PostgreSQL API tests with an older review task behind 205 newer tasks, durable history pagination/search, authentication on every new route, actual session state, and incremental logs. Linux Docker exercises symlink/FIFO rejection and replacement/cursor semantics. Frontend unit and browser regression tests cover the inspector, evidence, filters, and responsive layouts.

Remaining product capabilities require additional backend implementation: host power/snapshots, arbitrary interactive shell/files, live resource metrics, direct task creation/reordering, writeable dispatch settings, and a structured live tool-event stream. The frontend does not pretend these capabilities exist. Real GitHub/provider execution requires a configured manager and was not exercised in this validation.

The original source audit follows as the historical baseline.

Audited 2026-09-05 against commit `0dabc70` on `codex/agentic-os-frontend`.

## Verdict

The frontend covers the core operator workflow, but does **not** fully use the backend. Existing API capabilities are missing from the interface, some returned information is only available as raw JSON, and several views depend on a partially loaded task list. The larger agentic-OS vision also needs new backend APIs: live tool activity, dispatch configuration, and VM administration are not exposed today.

This was a source-level audit of the active Svelte application, REST routes and schemas, and the relevant orchestration, quota, runner, and result contracts. Two independent read-only subagent reviews covered API coverage and internal workflows. The configured default manager target (`127.0.0.1:8420`) did not answer the health probe; no real GitHub/provider execution was exercised during this audit. The prior frontend test results do not establish live backend integration completeness. No application behavior was changed by this audit.

## Existing capabilities that are connected

- Runs and run details; project onboarding and project listing.
- Accept/reject review, cancel nonterminal work, and requeue parked work. UI action visibility matches the backend state guards.
- Authenticated artifact downloads, run events, attempts, error text, and the structured result (currently raw JSON).
- Quota consumption, reservations, ceiling, available headroom, and exhaustion time.
- Docker host inventory and Werft-labelled container inventory.
- Polled activity snapshots, global status counts, manager process activity, session attendance, heartbeats, leases, deadlines, and scheduled retry timestamps.
- Authentication expiry recovery, transient activity failures, and task-detail polling.

There are 14 authenticated API route patterns: eight GET routes and six POST routes. The active UI reaches 12: it does not call the project lifecycle flip route or the separate artifact metadata-list route. Counting endpoints alone overstates the completeness of the integration.

Sources: `manager/werft/api/routes.py`, `manager/werft/api/system.py`, `manager/werft/app.py:580`, `dashboard/src/App.svelte`, `dashboard/src/lib/api.ts`, `dashboard/src/lib/RunInspector.svelte`, `dashboard/src/lib/ActivityMonitor.svelte`.

## Findings, in implementation order

### 1. Task filtering and review counts are incomplete beyond loaded pages

**High impact; existing API can address status/project filtering.**

`GET /runs` supports `status`, `project`, `limit`, and `offset`, including a total for the filtered query. The frontend always loads unfiltered 200-row pages and then filters those records locally. Review counts, the review empty state, project task lists, and command search can therefore miss matching older tasks. The loaded-count footer discloses the limitation, but does not make the results complete.

Use server-filtered queries for review, individual statuses, and projects, with separate state for each query's pagination. Combined status categories need multiple queries or a backend multi-status filter. Full-text search across all history needs a backend search parameter or an explicitly scoped loaded-results experience; the existing API has no text-search parameter.

Evidence: `manager/werft/api/routes.py:165`; `dashboard/src/App.svelte:74`, `:238`, `:783`.

Acceptance check: place a review task behind more than 200 newer runs and verify that Review still finds it and reports the correct count without manually loading unrelated tasks.

### 2. The inspector conflates session number and failure budget

**Correctness issue; existing fields can address it.**

The inspector displays the latest `attempt_no` as “Attempt N of max_attempts.” The backend deliberately treats lifetime attempt numbering separately from `attempt_count`: quota-exhausted attempts do not consume the failure budget, and requeue resets the budget without deleting history. A later session can therefore show an apparently impossible “Attempt 7 of 3.”

Show “Session #7” separately from “Failure budget: 1 of 3 used,” using the backend's budget counter. Explain that requeue grants a fresh budget. Do not use attempt number as completion progress.

Evidence: `dashboard/src/lib/RunInspector.svelte:226`; `manager/werft/quota/ledger.py:157`; `manager/werft/orchestrator/finalize.py:336`, `:359`; `manager/werft/api/routes.py:942`.

### 3. Manual project lifecycle switching has no UI

**Missing operator control; existing API.**

`POST /projects/{id}/flip` accepts `bootstrap` or `oracle_gated`. The Projects page reports the lifecycle but cannot change it. This route also changes GitHub branch protection; it is not just a visual preference.

Expose it in project settings as a deliberate advanced action, explain the review/CI and branch-protection consequences, and handle the documented 409/503 failures. It should not be a casual unexplained switch.

Evidence: `manager/werft/api/routes.py:1049`; `manager/werft/orchestrator/ci_watch.py` (`flip_project`); `dashboard/src/App.svelte:853`.

### 4. Evidence is downloadable, but the review experience underuses it

**Existing API/data; substantial presentation opportunity.**

The backend retains outputs, agent logs, test reports, and configured egress logs. `RunDetail.result` can carry the result envelope, provider usage, commit information, and errors. The active inspector exposes this as a generic JSON disclosure and a download list. It does not provide an inline log/diff/report viewer, structured result/usage summary, base/merge commit references, artifact collection timestamps, or integrity hashes.

The separate `/runs/{id}/artifacts` route exposes hashes; the embedded run-detail artifact list does not. An older `RunRow.svelte` calls that route but is not part of the active App component tree. Similarly, an unused component is not evidence of shipped capability.

Prioritize a readable outcome summary and safe text/diff preview, with files grouped by purpose and raw data retained in drilldowns. Treat collected HTML as untrusted content rather than inserting it into the application's origin. Token/cost data is observational and may be missing; it must not be presented as the admission budget.

Evidence: `manager/werft/api/schemas.py:53`, `:82`, `:110`; `manager/werft/api/routes.py:459`, `:559`; `manager/werft/orchestrator/driver.py:677`; `manager/werft/contracts/result.py`; `manager/werft/orchestrator/evidence.py:44`; `dashboard/src/lib/RunInspector.svelte:333`.

### 5. VM-to-task navigation fails for tasks outside the loaded list

**Existing API; completeness issue.**

The machine panel only offers Inspect if it finds the container's run in the currently loaded frontend array. The backend already returns `run_id` and supports fetching that task directly. A live environment associated with an older run can thus lose its drilldown even though the task is available.

Use the direct-by-ID inspection path already used by Activity, and expose the reported container image in technical details. Also distinguish running-container occupancy from scheduler capacity: dispatch admission uses live driver count, so a container count is not the complete admission decision.

Evidence: `dashboard/src/lib/MachinePanel.svelte:92`; `dashboard/src/App.svelte:135`; `manager/werft/api/system.py:20`; `manager/werft/orchestrator/dispatch.py:99`.

### 6. Quota diagnostics omit available provider readings

**Existing API; missing explanatory detail.**

The active quota panel uses the manager's time ledger and shows the last reading timestamp, but omits `last_reading_utilization`, `last_reading_source`, and `exhausted_source`. Those fields help explain why a provider blocks dispatch even when the ledger appears to have headroom. `QuotaStrip.svelte` renders some of these fields but is not mounted in the current app.

Show provider observations and their source/time in a quota drilldown, distinctly from the ledger's wall-clock budget. Preserve unavailable values rather than substituting zero.

Evidence: `manager/werft/api/routes.py:625`; `manager/werft/api/schemas.py:92`; `dashboard/src/lib/QuotaPanel.svelte`.

### 7. Useful mutation responses and error details are discarded

**Existing API; reliability and diagnostics improvement.**

Run mutations return an authoritative updated `RunSummary`; frontend action helpers type the result as `void` and reload all workspace data instead. The API helper also discards the response's error detail, so concrete backend explanations become generic failures.

Consume the mutation result immediately to update the selected task and list, then refresh dependent counters. Decode bounded API error messages safely and present actionable reasons, while retaining special handling for authentication and state conflicts.

Evidence: `manager/werft/api/routes.py:712`, `:749`; `dashboard/src/lib/api.ts:41`, `:56`, `:101`; `dashboard/src/App.svelte:279`.

## Capabilities that require backend API work

| Capability | What exists today | What is missing |
| --- | --- | --- |
| Live agent commands and logs | Internal bounded `read_log_tail`; retained outputs collected as artifacts | Authenticated incremental log/event API, offset or cursor semantics, and a safe viewer. No live command stream is currently exposed. |
| Full activity history | Latest 25 cross-run events and up to 200 active runtime records; full events on individual run detail | Paginated cross-run event history and per-run runtime access beyond the snapshot limit. UI pagination only pages the loaded snapshot. |
| Model/provider/dispatch settings | Validated file-based per-project dispatch configuration and provider registry | Read/write configuration API, validation feedback, and capability reporting. Settings is currently connection/preferences UI. |
| Direct task creation and queue control | GitHub ready-label intake; the UI opens a prefilled GitHub issue | Direct task creation, priority editing, queue reordering, and pause/resume endpoints. |
| Rich CI and project audit | CI SHA/conclusion in run events; project lifecycle/protection events in the database | Structured checks/check links and project-event history endpoints. |
| VM administration | Read-only Docker host and Werft container inventory; task cancellation triggers asynchronous cleanup | Host power, snapshots, arbitrary shell/file access, live resource metrics, and broader VM control APIs. |
| Deep readiness diagnostics | `/healthz` returns a simple OK; activity and system expose narrower health signals | A structured account/provider/GitHub/config readiness report. Healthz alone does not prove dispatch readiness. |

Sources: `manager/werft/runner/outputs.py:69`; `manager/werft/api/routes.py:78`, `:200`; `manager/werft/config/dispatch.py`; `manager/werft/contracts/task.py`; `manager/werft/orchestrator/ci_watch.py:102`; `manager/werft/api/system.py`; `dashboard/src/App.svelte:104`.

## Recommended sequence

1. Fix list/review completeness, session-budget wording, and VM-to-task drilldowns.
2. Expose project lifecycle control, readable evidence/results, and quota diagnostics using existing APIs.
3. Add a read-only live log/runtime API and capability/readiness reporting before expanding the dashboard's settings.
4. Design VM administration and direct task controls as explicit backend capabilities, then integrate them into the frontend.

Keep the established hierarchy: summary first, then task/session, then evidence and technical detail. Completeness should come from dependable drilldowns and complete queries, not by placing every field on the Overview page.
