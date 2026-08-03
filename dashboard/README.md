# Werft operator dashboard

A single-page Svelte 5 app for operators: a token-gated view of the runs list
(state, project, issue, attempt outcomes, PR/artifact links, and
accept/reject/cancel/requeue actions) plus a quota strip showing consumed,
reserved, ceiling, and headroom per provider account. It polls the manager's
`/api/v1` endpoints every 10 seconds and refetches immediately on a 409
(state changed under you) response from any action.

Build it with `npm ci && npm run build`; the static output lands in `dist/`,
which the manager serves via the `WERFT_DASHBOARD_DIST` environment variable
(point it at this directory's `dist/`).

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
