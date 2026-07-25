# Werft

**A self-hosted agentic OS for centrally managed, unattended coding projects.**

*Werft* (German: shipyard). Agents build in contained slips, every vessel passes sea trials, and only proven ships launch. Nothing moves on opinion.

## What it is

Werft is the successor to [Claude Agent Station](https://github.com/kenhaesler/claude-agent-station) (archived 2026-06-05 — see [the v1 verdict](docs/lineage/v1-verdict.md)). It runs coding agents unattended across multiple projects, dispatching work to whichever AI provider fits the task, and merges nothing that has not been proven by an executed oracle.

## Doctrine (founding decisions)

These five decisions came out of the v1 post-mortem and are load-bearing. Changing one requires updating this section deliberately, not drifting.

1. **Verification is executed, never judged.** The only merge gate is a green CI pipeline (tests + build + lint) run in a clean container against the merged result. No LLM verdict ever merges anything. Red or untestable work parks for a human. (v1 died here: its manager LLM read truncated diffs, executed nothing, and approved a branch with failing tests.)
2. **Blast radius is contained by branch topology.** Agents branch off a long-lived `unattended` integration branch per project. Green CI auto-merges land there — never on `main`. Promotion `unattended → main` is a human-triggered, dashboard-visible batch PR that re-runs the full pipeline against `main`.
3. **Providers are subscription CLIs, dispatched at the process layer.** Claude, Codex, and Kimi CLIs — plus a backend-neutral **local tier** (Aider pointed at a self-hosted OpenAI-compatible endpoint: vLLM / LiteLLM / Ollama) — run headless inside ephemeral runner containers behind thin adapters (~100 lines each: start task, stream log, exit code). The manager meters plan quotas (rolling windows, weekly caps, plus operator-set usage ceilings); the local tier is the free overflow. No gateway, no per-token billing in the core path (an optional per-provider credential gateway is a documented security choice, not the default — see ARCHITECTURE §6.6).
4. **Routing is a static YAML table, plus outcome recording.** Task labels/language/size map to an ordered provider preference; quota exhaustion falls through the chain. Per-provider outcomes (CI pass rate, retries, duration) are recorded from day one, but no learned router is built unless the data proves the table wrong.
5. **The backlog is human-fed only.** The manager works only issues you label for it. No self-generated work. (v1's analyst filed 89+ of its own issues and outran all verification capacity.)

## Architecture (approach A — thin manager over provider CLIs)

- **Manager service** — task queue, routing table, quota meter, dispatcher, promotion workflow. One engine, one language, one source of truth for run state (a run's status is a row; everything else derives from it).
- **Runner containers** — one ephemeral Docker container per run: clone of the target repo + one provider CLI. Completion is a structured signal (exit code / result file), never prose-matching.
- **CI oracle** — GitHub Actions on GitHub-hosted runners (keeps semi-untrusted, agent-authored execution off the Werft VM). Werft never implements verification; it only consumes green/red.
- **Dashboard** — deliberately small. It observes the loop; it must never outgrow it.

## Deployment target

A **dedicated virtual machine** (Rocky Linux) running the full stack via Docker Compose. Development happens here; the VM is the sole production environment, so the agents' blast radius ends at the VM boundary.

## Anti-goals (lessons paid for in v1)

- No second execution engine, ever. Port patterns, not parallel implementations.
- No LLM-judgment gates anywhere in the merge path.
- No agent-initiated replatforming of Werft's own substrate.
- No state outside the database: no sentinel files, no labels-as-locks, no `/tmp` handoffs.
- The dashboard serves the loop, not the other way around.

## Status

**Groundwork specified.** The full system architecture — schema, state machine, runner contract, routing/quota, git topology, deployment — is in [ARCHITECTURE.md](ARCHITECTURE.md) (v1.4; verified by three independent adversarial passes and a 2026/2027-currency-and-completeness audit — see [docs/lineage/architecture-2026-currency-audit.md](docs/lineage/architecture-2026-currency-audit.md) — structurally locked by `tests/architecture_spec.test.mjs`). No implementation yet.
