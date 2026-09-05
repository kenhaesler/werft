# Werft agent workspace

A responsive Svelte 5 workspace built with TypeScript, Vite 8, Tailwind CSS 4,
and a self-hosted Geist variable font. Includes active agents, searchable run
history, review actions, an evidence inspector, projects, Docker host inventory,
provider quotas, settings, and a keyboard command menu (`Ctrl/⌘ K`).

With no saved token, the app opens a clearly labeled, interactive sample
workspace. Preview actions only modify browser memory; they never call the
manager or GitHub. Connect with a manager token to switch to live data. Tokens
are stored in localStorage and removed by Settings → Disconnect. Updates poll
every 10 seconds while the tab is visible. Conflicts refetch the manager state.

## Local development

Use Node 24.15 or newer (the jsdom test dependency requires this patch floor):

```sh
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. The Vite development server proxies `/api` to
`http://127.0.0.1:8420`; set `WERFT_API_TARGET` to use another manager. In production,
the UI uses the same origin as the manager. No third-party font requests are needed.

Build it with `npm ci && npm run build`; the static output lands in `dist/`,
which the manager serves via the `WERFT_DASHBOARD_DIST` environment variable
(point it at this directory's `dist/`).

## Backend integration

- Runs, attempts, events, artifacts, and accept/reject/cancel/requeue use the
  existing `/api/v1/runs` contract. Artifact downloads attach the bearer header.
- `GET /api/v1/projects` lists repositories, including those with no runs.
  Add project uses the existing onboarding endpoint.
- New task opens a prefilled GitHub issue with `werft:ready`; the operator still
  submits the issue in GitHub. Task creation is issue-driven; there is no direct-dispatch API.
- `GET /api/v1/system` reports the configured Docker host's name, operating
  system, architecture, CPU count, memory capacity, engine version, concurrent
  run limit, and Werft-labelled containers. The manager filters Docker output;
  unrelated containers and sensitive inspect fields are not returned.
- The socket proxy needs `INFO=1` and `VERSION=1`, included in `deploy/compose.yaml`.
  Docker errors show an unavailable state without disabling the rest of the UI.
- VM management currently means host inventory and workload inspection/cancellation
  through the existing run state machine. Host reboot, snapshots, live utilization,
  and interactive shell access require additional backend capabilities; the UI
  explains their absence instead of simulating them.

## API coverage and deployment requirements

The dashboard uses the manager's authenticated `/api/v1` routes. Preview mode
never substitutes sample data for a failed live response; unavailable and stale
states remain visible.

| Dashboard surface                   | Manager routes                                                                           |
| ----------------------------------- | ---------------------------------------------------------------------------------------- |
| Projects and agent canvas           | `GET /projects`, paginated `GET /runs` with status filters                               |
| Project onboarding and lifecycle    | `POST /projects/onboard`, `POST /projects/{id}/flip`, `GET /projects/{id}/events`        |
| Activity and history                | `GET /activity`, `GET /events`                                                           |
| Task drilldown                      | `GET /runs/{id}`, `GET /runs/{id}/runtime`, cursor-based `GET /runs/{id}/log`            |
| Review and recovery                 | `POST /runs/{id}/review/accept`, `/review/reject`, `/cancel`, `/requeue`                 |
| Evidence                            | `GET /runs/{id}/artifacts`, authenticated file downloads and bounded byte-range previews |
| VM inventory                        | `GET /system`; workload actions use the run state machine                                |
| Usage and configuration             | `GET /quota`, `GET /capabilities`                                                        |
| Orchestrator and agent conversation | `GET /conversations/{scope}`, `POST /conversations/{scope}/messages`                     |

Conversation scope is `orchestrator` or a run UUID. Sending uses a stable client
message ID for safe retries; polling displays recorded replies and delivery status.
Orchestrator replies require `WERFT_CONVERSATION_API_KEY_FILE` and
`WERFT_CONVERSATION_MODEL`. Agent steering additionally requires
`WERFT_AGENT_CONVERSATIONS_ENABLED` and a compatible runner transport; availability
comes from the conversation endpoint. See `deploy/compose.conversations.yaml`.
Questions and directions reach an agent at supported turn boundaries, not as an
interactive terminal interrupt.

A successful API connection does not prove GitHub, provider credentials, or Docker
are ready: Settings reports configuration separately from verification. New tasks
continue to GitHub for submission. Host reboot, snapshots, and interactive shells
are not exposed by the manager and are not represented as working dashboard actions.

The integration audit exercised backend routes against PostgreSQL in Docker and
browser contract tests against controlled HTTP fixtures. It did not send a paid
model request or create a real GitHub issue. Windows skips POSIX-only no-follow,
FIFO, and symlink tests when those facilities are unavailable.

## Verification

```sh
npm run lint
npm run check
npm run format:check
npm test
npm run build
npx playwright install chromium
npm run test:e2e
```

Playwright covers preview task/project creation, navigation, keyboard focus,
evidence download, live authentication, review conflicts, disconnect, and mobile
overflow. It writes desktop/mobile screenshots under `.impeccable/review/`.

## A note on the TypeScript pin

`typescript-eslint`/`@typescript-eslint/parser` do not yet run against real
TypeScript 7.x (as of 2026-08-03 they hard-error, and bypassing that gate
crashes with an internal API mismatch instead). Per TypeScript's own
[TS 7.0 migration guidance](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/),
this project keeps the ESLint-facing `typescript` package aliased to the
`@typescript/typescript6` compatibility shim (so lint tooling has a working
6.x-API surface to parse against), and pins the real TypeScript 7.0.2
compiler separately as `typescript-native`. Nothing in the build pipeline
(Vite/esbuild, the Svelte compiler) depends on either package's version, so
this only affects editor/lint tooling, not the shipped app.
