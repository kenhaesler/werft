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
  submits the issue in GitHub. There is no invented chat or direct-dispatch API.
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
