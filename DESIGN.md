---
name: Werft
description: A connected operational canvas for supervising approved agent work and its host environment.
colors:
  canvas-light: "#f7f9fc"
  sidebar-light: "#ffffff"
  panel-light: "#ffffff"
  panel-hover-light: "#eef4ff"
  border-light: "#dce5f0"
  text-light: "#172b4d"
  muted-light: "#52657e"
  accent-light: "#2463eb"
  warning-light: "#97601b"
  canvas-dark: "#10131d"
  sidebar-dark: "#141824"
  panel-dark: "#191e2c"
  panel-hover-dark: "#242c40"
  border-dark: "#30394e"
  text-dark: "#edf1ff"
  muted-dark: "#a8b4cc"
  accent-dark: "#8ca9ff"
  warning-dark: "#f2bb72"
  selection-lavender: "#c4cdff"
  orb-deep: "#060b5c"
  orb-cobalt: "#0b2ef9"
  orb-cyan: "#0ad4ff"
  orb-violet: "#7d1cf8"
typography:
  display:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "34px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.035em"
  title:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "16px"
    fontWeight: 560
    lineHeight: 1.4
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "14px"
    lineHeight: 1.6
  mono:
    fontFamily: "Cascadia Code, SFMono-Regular, Consolas, monospace"
    fontSize: "12px"
rounded:
  control: "10px"
  panel: "16px"
  canvas: "22px"
spacing:
  control-gap: "8px"
  field-padding: "10px 12px"
  button-padding: "10px 13px"
components:
  button-primary:
    backgroundColor: "{colors.accent-light}"
    textColor: "#ffffff"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "{spacing.button-padding}"
  button-primary-hover:
    backgroundColor: "#1d50c4"
  canvas:
    backgroundColor: "{colors.canvas-light}"
    textColor: "{colors.text-light}"
    rounded: "{rounded.canvas}"
---

## Overview

Werft is a quiet operational workstation for a self-hosting operator supervising approved autonomous software work. The visual center is a living connected canvas: the fleet view places project tiles beneath a Werft Orchestrator hub, and a project view connects the project hub to its live task and agent workstreams. The approved Werft logo geometry and lettering remain unchanged.

The hierarchy is Projects → project canvas → task details. Human review and attention signals lead active work; activity and infrastructure remain supporting context. This document records the implemented system in `dashboard/src`, with `PRODUCT.md` as the product truth.

Visual references for the direction were United Carriers, Dribbble shot 27705083, Dribbble shot 27706179, the [abstract fluid motion study](https://cdn.dribbble.com/userupload/48929775/file/108239e0f46eab8e4d119f72955d098e.mp4), and the [agentic interaction study](https://dribbble.com/shots/27274138-Agentic-experience-interaction-for-mobile-OS). They informed the connected, calm operational tone, living flowing layers, and JARVIS-like orb presence; no raster assets from those references are copied into the product.

## Colors

Light mode is predominantly white and cool blue-gray. `--canvas` frames the workspace, `--panel` and `--surface-raised` carry content, `--border` separates regions, and `--text`/`--muted` establish reading hierarchy. `--accent` is reserved for primary actions, active navigation, links, focus, progress, and selection.

Dark mode is a deliberate semantic remap rather than an inversion: `#10131d` canvas, `#191e2c` panels, `#30394e` borders, `#edf1ff` text, and lavender-blue `#8ca9ff` accent. Selected workstreams use a high-contrast lavender panel (`#c4cdff`) with dark ink in dark mode so selection remains obvious.

Amber (`--amber`, `#97601b` light / `#f2bb72` dark) is the attention and interruption signal: review, waiting, quota, failed, parked, and blocked states. Status color is always paired with a readable status label. Danger uses the semantic salmon values in `theme.css`; it is not a brand accent.

## Typography

Geist Variable is bundled locally and used for headings, navigation, controls, labels, and body copy. Use the system monospace stack for branches, identifiers, logs, payloads, and keycaps. The canvas display heading is 34px with tight tracking; project and task titles are compact but allowed to wrap anywhere. Supporting labels are generally 12–14px. Do not turn body text into the old 12px universal rule: inspector copy, settings help, and state explanations need readable 14px text.

## Layout

The shell keeps a left navigation rail and a flexible workspace. The canvas is a bordered, rounded internal viewport with its own overflow for larger projects. At fleet level, project tiles connect upward to the `Werft / Orchestrator` hub, whose `Ask or steer work` action has an `href="#talk"` affordance but prevents the hash default and calls the app's `ontalk` callback, which routes to Talk. Inside a project, a project keel connects downward to task workstreams. Each workstream exposes the real pipeline: Task, Agent, Checks, Result.

Connections are measured from rendered DOM geometry with `ResizeObserver` and emitted as SVG cubic paths. The SVG layer is decorative and pointer-transparent; it must follow actual hub/tile positions after resize and after task selection rather than using fixed coordinates.

The canvas uses container queries. At roughly 650px of canvas width, fleet tiles reduce padding and height, the heading stacks, the hub tightens, and the portfolio becomes a narrower single-column-friendly grid. The project workbench tightens its padding, hides the open-task count, and keeps task cards readable. The viewport removes its desktop max-height at this size so mobile inspection can scroll naturally. Global inspector tabs wrap below 600px. Preserve a minimum usable body width of 360px.

## Elevation & Depth

Tone, one-pixel borders, and restrained localized shadows provide most depth. Fleet tiles use a cool gradient and a small lift on hover; the project keel and task workstreams use shallow shadows. The canvas dot/grid treatment and perspective tile symbol are atmospheric structure, not data visualization.

The fleet hub, project keel, and conversation presence header are translucent scene chrome: a `color-mix` panel surface, 24px backdrop blur for canvas chrome (20px for the conversation header), and a soft inset highlight. A `@supports` fallback returns these surfaces to an opaque panel. Keep body copy, messages, controls, and evidence on solid reading surfaces for contrast.

Route changes use the native View Transitions API through the `werft-workspace` transition name. The implemented transition scales and fades the workspace over 260ms. Component hover transitions are short (about 180–240ms). Every motion path has a `prefers-reduced-motion: reduce` rule that removes transitions, hover lift, and view-transition animation. Do not add persistent animation that competes with live status.

`WerftOrb.svelte` is a native WebGL animated light field with a soft blue core, diffuse cyan edges, and independently drifting translucent halos, plus a CSS fallback when WebGL compilation or context support fails. The decorative light field flows continuously by default; the shader's energy response is reserved for an actual connected request and never implies backend activity. Animation pauses for reduced motion, when the document is hidden, and when its stage is offscreen via `IntersectionObserver`. Both fleet and Talk expose an explicit play/pause control.

## Shapes

Use compact rounded rectangles: 10px controls, 16px cards and hubs, and 22px canvas viewport. Keep borders quiet and consistent. Status dots remain circular; icons sit in small inset rounded containers. Focus-visible controls use the existing blue outline and offset. Avoid adding a new radius for individual components.

## Components

**Fleet and project canvas.** Project tiles summarize owner, repository, active tasks, waiting work, and attention count. Attention tiles receive amber border and icon treatment. Selecting a project opens the connected project workbench. Selecting a task opens its inspector beside the canvas while preserving the canvas context.

**Werft presence orb.** The fleet hub uses a 72px orb, animated by default, with its own pause control. The orchestrator Conversation header uses a 168px orb on desktop and 104px on mobile. The header shows the real conversation state and provides a play/pause button; an actual request changes shader energy only while a connected message is being sent.

**Task workstream.** A task card has a task endcap and three operational pieces for Agent, Checks, and Result. The route labels communicate the current backend lifecycle stage. Running work gets a live signal; selected work gets the lavender/high-contrast treatment. Empty states explain what approved action creates work.

**Run progress.** `RunProgress.svelte` presents Preparing → Working → Checks → Review → Completed. Passed stages use accent; the current stage is filled accent; interrupted statuses use amber and explicitly say execution stopped. A merging run labels the final stage Merging. Never render invented percentages or imply completion when the backend has not reported it.

**Inspector and evidence.** Inspectors preserve the last successful detail through transient reconnect failures and display stale state plus update time. Timeline, Evidence, Attempts, and Conversation stay distinct. Backend status, events, artifacts, and lifecycle actions remain authoritative; unavailable capabilities are labeled unavailable, and preview data is labeled illustrative.

**Theme and navigation.** `ThemeToggle` switches the semantic token map and persists the preference. Navigation and controls retain keyboard focus, accessible names, and current-page semantics. The existing logo is a fixed brand asset and should not be redrawn or restyled as a generic icon.

## Do's and Don'ts

- Do keep the operational canvas visibly connected and let measured SVG links follow real layout geometry.
- Do preserve both light and dark semantic token maps, with clear contrast in selected lavender workstreams.
- Do use amber for attention and interruption, and pair every status color with text.
- Do preserve backend lifecycle status, evidence, stale state, and unavailable capabilities.
- Do make preview/demo content explicit and keep the `Werft / Orchestrator` fleet hub linked to `#talk`.
- Do respect native View Transitions and reduced-motion preferences, including WebGL pause behavior when reduced motion, hidden, or offscreen.
- Do keep glass effects in chrome and keep dense body text on solid surfaces; retain an opaque-panel fallback where backdrop blur is unavailable.
- Don't claim a task completed, merged, monitored, or changed infrastructure without backend evidence.
- Don't replace the canvas with disconnected dashboard cards or fixed-position connector lines.
- Don't introduce green/olive as brand colors, copy external raster assets, or alter the approved logo.
- Don't use decorative orb motion to claim idle, demo, or fictional backend activity, and don't make the fleet hub a hash-only route.
