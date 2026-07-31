# Werft

**A self-hosted agentic operating system for one operator's software projects.**

Werft (German: *shipyard*). You hand it a VM; the VM belongs to Werft. Agents build in capable, disposable containers; proven work merges; everything leaves an evidence trail.

Werft is the successor to Claude Agent Station (v1, archived 2026-06-05) — rebuilt around the one lesson v1 died for, and realigned 2026-07-31 around what the operator actually wants ([the design record](docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md)).

## What it does

Werft runs coding agents unattended across projects, routes each kind of work to the model you chose on the subscriptions you already pay for, protects your quota using the providers' own reported limits, and merges nothing unproven: **code merges only on green CI executed against the merged result; everything else lands only when you accept it — until a work type earns automation.** Every run leaves a first-class evidence trail: what was done, what it cost, what the agent saw.

The dev factory is the spine; the OS-feel — capable environments, per-project memory, scheduled work, non-code work — is the body it grows.

## Doctrine

These six decisions are load-bearing. Every design choice traces back to one of them.

1. **Verification is executed for code, human-gated for everything else.** The merge gate for code is green CI in a clean environment against the merged result. No LLM verdict ever merges code — v1 died here. Non-code work and bootstrap-phase projects pass through the operator's review queue, with a per-work-type path to automated acceptance once proven.
2. **Blast radius is contained by branch topology plus disposable containers.** Agents work on branches off a long-lived `unattended` branch; green-CI merges land there, never on `main`. Promotion to `main` is a human-triggered batch PR. The container is the wall: capable dev boxes (root inside, installs, services, browser) with pragmatic hardening, scoped short-lived credentials, and per-run egress rules. Residual container-escape risk is accepted and written down, not engineered away.
3. **Providers are subscription CLIs on the operator's personal accounts,** dispatched at the process layer. Claude Code first; Codex, Kimi, and a local OpenAI-compatible tier next; Grok and Gemini later. No gateway, no per-token billing in the core path.
4. **Quota truth is provider-reported.** Each adapter reads its provider's own usage and limit signals, and those numbers rule dispatch admission; Werft's own metering ledger fills gaps and estimates between readings. Self-capping below the operator's ceiling is the #1 feature.
5. **The backlog is human-approved.** Agents may propose issues; nothing is dispatched without the operator's label. v1's failure — self-dispatched work flooding verification capacity — stays structurally impossible.
6. **Evidence is a product surface.** Runs collect artifacts by default — transcripts, diffs, screenshots, browser traces — into a per-run record with size caps.

## How a run works

1. You (or, later, an approved agent proposal) label a GitHub issue `werft:ready` — the only intake path.
2. The manager claims the run and reserves quota in one transaction, prepares a workspace, and starts one ephemeral capable container with the chosen provider CLI.
3. The agent works — installs what it needs, runs what it builds — commits, and pushes. The adapter reports completion by exit code and `result.json`; artifacts are collected.
4. The manager opens a PR onto `unattended`. **Oracle-gated** projects wait for the executed CI check (`werft-oracle`) on the merged result: green auto-merges, red retries fresh while budget lasts, then parks. **Bootstrap** projects (no CI yet — their early runs exist to build it) wait in your review queue instead; first green CI flips the project to oracle-gated.
5. You promote: a batch PR `unattended → main`, CI re-runs, you merge.

## Status

**Clean slate as of 2026-07-31.** The prior groundwork (architecture v1.4, ten-phase build plan, discovery record) is archived in [`docs/lineage/`](docs/lineage/README.md). The current buildable specification is [`SPEC.md`](SPEC.md), scoped to the thin loop: the greenfield Elastic log-analysis project goes from empty repository to its first oracle-gated merge, driven end-to-end by Werft. No implementation yet — the thin-loop issues on this repo are the build.

## Anti-goals

- No second execution engine; Postgres is the queue, the event bus, and the metrics store.
- No LLM-judgment gates anywhere in the code-merge path.
- No agent access to Werft's own substrate: Werft is never an onboarded project of itself, and only Werft controls the VM.
- No state outside the database (plus the evidence files it indexes).
- The dashboard serves the loop; it never becomes the product.
