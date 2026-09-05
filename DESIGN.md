---
name: Werft
description: A quiet workstation for supervising agent work and its host environment.
colors:
  canvas: "#111312"
  sidebar: "#151715"
  panel: "#191c19"
  panel-hover: "#202520"
  border: "#2a2f2a"
  text: "#e7eae7"
  muted: "#9aa29a"
  dim: "#848e84"
  mint: "#b8e5a0"
  mint-ink: "#172311"
  amber: "#ddb889"
  primary-hover: "#cbefb6"
  button: "#222720"
  button-text: "#d7dfd2"
  input: "#141714"
typography:
  headline:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "clamp(28px, 2.7vw, 39px)"
    fontWeight: 520
    lineHeight: 1.25
    letterSpacing: "-0.035em"
  title:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "15px"
    fontWeight: 560
    letterSpacing: "-0.015em"
  body:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "12px"
    lineHeight: 1.6
  button:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "11px"
    fontWeight: 520
  mono:
    fontFamily: "Cascadia Code, SFMono-Regular, Consolas, monospace"
rounded:
  keycap: "4px"
  navigation: "6px"
  control: "7px"
  callout: "9px"
  panel: "12px"
  dialog: "14px"
spacing:
  control-gap: "8px"
  field-padding: "10px 12px"
  button-padding: "10px 13px"
  card-padding: "19px 19px 0"
  page-padding: "37px 36px 24px"
components:
  button-primary:
    backgroundColor: "{colors.mint}"
    textColor: "{colors.mint-ink}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "{spacing.button-padding}"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.button}"
    textColor: "{colors.button-text}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "{spacing.button-padding}"
  input:
    backgroundColor: "{colors.input}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "{spacing.field-padding}"
---

# Design System: Werft

## Overview

**Creative North Star: "The Agent Workstation"**

A restrained operational workspace with charcoal and olive surfaces, pale sage actions, and warm review signals. Dense supporting information surrounds readable task titles; state and evidence carry more visual weight than decoration. This is a record of the implemented code, not an approved image composition.

The design supports an operator supervising approved work. Live observations, illustrative preview data, and unavailable capabilities remain visibly distinct. The source of truth is `dashboard/src/app.css` and the Svelte components; `PRODUCT.md` defines product constraints.

**Key Characteristics:**

- Tonal surfaces with quiet borders.
- Compact Geist typography and restrained sage emphasis.
- Work, review, and machine capacity visible together.
- Explicit state labels and keyboard access.

## Colors

The palette combines cool charcoal structure with olive surfaces and soft botanical accents.

### Primary

**Sage (`mint`)** marks primary actions, focus, and selection; its dark ink keeps filled buttons readable. Brighter sage appears on primary hover.

### Secondary

**Warm amber** identifies review, pending CI, and quota attention. Review callouts use a subdued brown-olive fill. Failed and parked statuses use muted salmon; running and merging statuses use pale green. Always accompany status color with text.

### Neutral

Canvas and sidebar establish the shell; panel and hover tones separate nested surfaces. Text, muted, and dim distinguish primary content from supporting metadata. Borders divide sections without adding heavy frames. Component-specific shades remain in the stylesheet rather than expanding the token vocabulary for every one-off color.

## Typography

Geist Variable is bundled through `@fontsource-variable/geist`, with a sans-serif fallback. The same family serves headings, navigation, and controls. Code, logs, branches, and keycaps use the system monospace stack; no remote font service is required.

The headline and section-title roles are recorded above. Task-card titles use a slightly larger title treatment (16px, weight 550, line-height 1.5); supporting text is usually 10–12px. Dense timestamps can fall to 9px, with selected responsive metadata at 8px. These are incumbent density choices, not a mandate to shrink new content. Body values represent descriptive copy rather than a universal body element size. Large headings use balanced wrapping and tight tracking; task names and evidence paths must accommodate long content.

## Layout

The desktop shell uses a sticky left navigation rail (232px) and a flexible main area. Content is centered with a maximum width (1680px). The overview places active agents and recent work centrally, with a machine and quota column (288px) separated by a gap (28px). Active agents form two equal columns with a gap (15px). This overview composition is a surface pattern, not a requirement for every page.

Observed responsive boundaries:

| Width | Behavior |
| --- | --- |
| At least 1550px | Larger page insets, agent cards, and a 315px inspector column. |
| At most 1250px | Rail narrows to 210px; inspector to 250px; run timestamps and summary scope are hidden. |
| At most 1050px | Overview inspector moves below work into two columns; machine page becomes one column. |
| At most 760px | Navigation becomes a 232px overlay drawer; topbar supplies its trigger; quota page stacks. |
| At most 480px | Active cards and overview insights become single columns; headings and settings wrap. |

The minimum supported body width is 360px. Spacing is intentionally compact and contextual, not a strict invented scale. Preserve readable content and control access when secondary metadata disappears.

## Elevation & Depth

Most surfaces use tone and one-pixel borders rather than shadows. Lift is reserved for modal dialogs and notifications. The CSS-built machine illustration adds a localized perspective shadow; it is decorative and hidden from assistive technology. Dialog backdrops darken and blur the workspace. Exact shadow, focus, motion, and breakpoint extensions live in `.impeccable/design.json`.

Links and buttons transition color, background, and border in 180ms. Modal entry is 250ms; the run inspector slides in over 300ms. Reduced-motion preference disables animation and transitions globally.

## Shapes

Small rounded rectangles establish a practical, compact form language. Controls and fields use the control radius; cards use the panel radius; navigation and keycaps are tighter. Review callouts and agent avatars use the intermediate callout radius. Status dots remain circular. Avoid introducing a new radius for each new component.

## Components

**Buttons.** Sage primary actions use dark text; secondary actions use an olive fill and outlined edge. Default minimum height is 36px, with a 30px small variant. Danger actions use salmon text and border, with a dark warm hover fill. Disabled controls dim to half opacity and retain native disabled behavior. Icon buttons have explicit accessible names.

**Inputs.** Dark inset fields use a quiet border, sage caret, and sage focus border. Labels accompany form fields; failures appear as readable inline text. All focus-visible elements receive a sage outline (2px) with offset (4px).

**Navigation.** Compact rows combine an icon, label, and optional count. The active row gains an olive fill and pale text, with `aria-current="page"`. On mobile, the drawer traps Tab, closes with Escape or its backdrop, restores focus to the trigger, and makes the background inert. The closed drawer is inert as well.

**Agent cards and review callout.** Whole-card buttons show status, task title, project, current stage, and compact run metadata. Hover lightens the surface and border. Warm review callouts make human decisions visible without overwhelming active work.

**Run list and inspector.** Rows open a native modal side panel, up to 640px wide and full viewport height. Timeline, Evidence, and Attempts use buttons with pressed state. Evidence supports authenticated downloads, loading, empty, and error states. Available lifecycle actions follow the run state; a workload is canceled through its run, not by inventing host controls.

**Dialogs and command menu.** Native `dialog.showModal()` provides modal behavior and Escape dismissal. Standard dialogs are 510px wide; command search is 580px, both constrained to the viewport. Ctrl/Cmd+K opens search and focuses its input. Results use Tab and Enter, as the footer states; this is not an arrow-key listbox.

**Machine and quota panels.** Show host inventory, container workloads, runner slots, and provider quota with text alongside bars and dots. Missing data produces an unavailable state and retry action. Preview carries a persistent banner and sample labels. Refresh failures warn that displayed data may be stale. Host power, snapshots, and arbitrary shells are unavailable; conversational task creation is illustrative in preview, with live work following approved issue dispatch.

## Do's and Don'ts

- **Do** keep primary actions sage, review attention warm, and ordinary surfaces quiet.
- **Do** pair color with status text, preserve visible focus, and respect reduced motion.
- **Do** keep preview, live, stale, empty, and unavailable states explicit.
- **Do** use real run evidence and capacity data to establish hierarchy.
- **Don't** imply that a sample task changed live infrastructure.
- **Don't** expose host reboot, snapshot, or shell actions without supporting APIs.
- **Don't** replace dense supporting content with oversized decorative dashboard cards.
