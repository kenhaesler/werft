# Werft identity realignment — design

**Date:** 2026-07-31
**Status:** Approved by the operator (interview session, 2026-07-31)
**Supersedes:** ARCHITECTURE.md v1.4, BUILD-PLAN.md v1.0, and the three product-discovery documents (all now in `docs/lineage/`) as sources of product truth. Those documents move to `docs/lineage/` as historical record and remain a reference mine (containment red-team findings, technology currency research) during implementation.

## Why this document exists

The repo reached v1.4 of a 119 KB architecture specification, a ten-phase build plan, and 14 GitHub issues — with zero lines of application code — and the operator no longer recognized the project in them. A structured interview (question by question, each answer recorded below) re-established what Werft is. The operator chose a **clean slate**: archive the old documents, write a fresh lean spec, cut new issues, and build.

The core drift being corrected: the discovery process had repeatedly "corrected" the operator's stated wants (quota transparency, evidence, session continuity, per-model routing, OS-feel) back into a verification-doctrine-shaped product, and reshaped or rejected three explicit wants (browser evidence, provider-authoritative limits, capable environments). This design restores the operator's intent while keeping the one lesson v1 actually died for: **no LLM verdict ever merges code.**

## Decision ledger (operator answers, 2026-07-31)

| # | Question | Decision | Overturns |
|---|---|---|---|
| 1 | Core identity | Autonomous dev factory that *feels* like a true agentic OS — "I give it a VM and then it belongs to Werft" | "Agentic OS is just positioning" |
| 2 | Agent environment | **Capable dev box**: root inside the container, system/language package installs, services, headless browser | Strict manifest/no-root/pre-baked design (old BUILD-PLAN P2, containment §2) |
| 3 | Escape-risk posture | **Pragmatic containment**: standard Docker hardening + scoped short-lived credentials + egress rules; residual runc/kernel-escape risk accepted and written down | I-1 as an engineered hard wall |
| 4 | Access model | **Tailscale stays** (dashboard/API never exposed; single bearer token remains defensible) | — |
| 5 | OS primitives in vision | **All four**: per-project memory between runs, scheduled/recurring work, non-code work types, agent-proposed backlog | Doctrine #5's total ban on agent-generated work |
| 6 | Non-code acceptance gate | **Human review always**, designed so a work type can be switched to automatic acceptance once it proves reliable | — |
| 7 | Quota truth | **Provider-reported numbers rule**; Werft's own ledger fills gaps and estimates mid-window | Ledger-first (T0-primary) design |
| 8 | Work evidence | **First-class surface**: per-run timeline of screenshots, browser traces, transcripts, diffs; collect-by-default with size caps | "Werft never drives a browser"; deny-by-default retention |
| 9 | Providers | Claude Code, Codex, Kimi, local tier; Grok and Gemini later. **Claude Code is priority #1** | Codex-first build order; Kimi absent from plan |
| 10 | Pilot project | **The AI-based Elastic log-analysis tool, greenfield** | pcapng-inspector pilot |
| 11 | Greenfield oracle | **Bootstrap mode**: a project's early runs build its harness+CI and are gated by operator review; oracle-gating switches on at first green CI | "The oracle exists before anything is built against it" |
| 12 | Path forward | **Clean slate**: archive to lineage, fresh lean README+spec, new issues, start coding | The v1.4 spec machinery, incl. the prose-regex test suite |
| 13 | First build shape | **Thin loop first** (option A) | — |

What survives from the old doctrine unchanged: executed-oracle merge gate for code; `unattended` branch topology with human-triggered promotion to `main`; Postgres as the only state; one manager process; static routing table with outcome recording; subscription CLIs on the operator's personal accounts.

## 1. Product statement (v2)

> **Werft is a self-hosted agentic operating system for one operator's software projects.** You hand it a VM; the VM belongs to Werft. It runs coding agents unattended in capable, disposable containers, routes each kind of work to the model you chose on the subscriptions you already pay for, and protects your quota using the providers' own reported limits. Nothing lands in a project unproven: **code merges only on green CI executed against the merged result; everything else lands only when you accept it — until a work type earns automation.** Every run leaves a first-class evidence trail: what was done, what it cost, what the agent saw.

The dev factory is the spine; the OS-feel (capable environments, memory, scheduling, non-code work) is the body it grows.

## 2. Doctrine v2

1. **Verification is executed for code, human-gated for everything else.** No LLM verdict ever merges code. Non-code work and bootstrap-phase projects pass through the operator's review queue, with an explicit per-work-type path to automated acceptance once reliability is proven.
2. **Blast radius is contained by branch topology plus disposable containers.** `unattended` branch, auto-merge only on green oracle, human promotion to `main` — unchanged. The container is now the wall, not the VM: capable dev boxes (root inside, installs, services, browser), pragmatic Docker hardening, scoped short-lived credentials, egress rules. Residual container-escape risk is accepted and recorded, not engineered away.
3. **Providers are subscription CLIs on the operator's personal accounts,** dispatched at the process layer. Claude Code is first-class and first-built; Codex, Kimi, and a local OpenAI-compatible tier follow; Grok and Gemini later. No gateway, no per-token billing in the core path.
4. **Quota truth is provider-reported.** Each adapter reads its provider's own usage/limit signals (CLI status output, usage APIs, headers) and those numbers rule admission decisions; Werft's own metering ledger fills gaps and estimates between provider updates. Self-capping against operator-set ceilings is the #1 feature.
5. **The backlog is human-approved** (was: human-fed). Agents may propose issues; nothing is dispatched without operator approval. v1's failure mode — self-dispatched work flooding verification capacity — remains structurally impossible.
6. **Evidence is a product surface.** Runs collect artifacts by default (screenshots, Playwright traces, command transcripts, diffs) into a per-run timeline with size caps.

## 3. System shape

Same skeleton as the v1.4 architecture, honestly re-scoped:

- **Manager:** one Python process (FastAPI, single uvicorn worker), PostgreSQL as queue + event bus + metrics store, single writer, state-machine-driven runs. No Redis, no broker, no metrics stack.
- **Runners:** one ephemeral Docker container per run, cold-started, destroyed after. **Capable images:** per-project dev boxes the agent may modify at runtime (dnf/apt, pip/npm, services, headless browser); rebuilt fresh each run so mutations die with the container. Egress allowlisted per project, including the package registries the project needs.
- **Code oracle:** GitHub Actions on the merged result; branch protection; polling only, zero inbound listeners.
- **Bootstrap mode (new):** a project lifecycle state. Runs on a bootstrap-phase project are gated by operator review instead of CI; the standing goal of early runs is to produce the project's harness and CI. First green CI flips the project to oracle-gated.
- **Review queue (new, core object):** holds bootstrap-phase outputs and (later) non-code outputs for operator accept/reject. Built so a work type can graduate to auto-accept.
- **Evidence store (new):** per-run artifact collection into the DB/filesystem with a browsable per-run view; timeline UI grows later.
- **Provider usage readers (new duty per adapter):** each provider adapter reads and reports its provider's own usage/limit data.
- **Dashboard:** Svelte, served by the manager, Tailscale-only, single static bearer token.
- **Designed-for but deferred:** per-project memory store, scheduler for recurring work, non-code work types, agent-proposed issues, providers beyond Claude Code.

## 4. First build — the thin loop

**Milestone: the Elastic log-analysis project goes from empty repository to its first oracle-gated merge, driven end-to-end by Werft.**

Order of work:

1. Manager spine: schema, run state machine, single-writer discipline, config.
2. Capable runner: per-project image build, container lifecycle, Claude Code adapter, egress rules, scoped credentials.
3. Bootstrap mode + review queue: dispatch a run against the empty Elastic repo, operator reviews and accepts the harness/CI PRs.
4. First green CI on the Elastic repo → project flips to oracle-gated → first unattended proven merge.

In scope, minimally: one quota ceiling with provider-reported data where Claude Code exposes it; artifacts kept and browsable per run; a bare runs list as the only UI. Out of scope for the milestone: timeline UI, second provider, memory, scheduler, non-code work, promotion UI polish.

## 5. Repo transition

1. Commit the currently uncommitted material (BUILD-PLAN.md, `docs/product-discovery/`, README status edit) so nothing is lost, then move ARCHITECTURE.md, BUILD-PLAN.md, product-discovery docs, and `tests/architecture_spec.test.mjs` under `docs/lineage/`.
2. Write a fresh short README (identity + doctrine v2) and one lean SPEC.md scaled to the thin loop.
3. Close the 14 existing GitHub issues with a pointer to this design; cut new issues from the thin-loop milestone.
4. Do the work on a fresh branch off `main`; merge the wip branch's historical material first.

## Out of scope for this design

- Detailed schemas, API shapes, and container hardening flag lists — the lean SPEC.md and implementation decide these, mining the lineage documents where their research still applies (the containment red-team and 2026 currency findings remain largely valid inputs).
- Resolution of the personal-account / provider-ToS tension (old §13 risk #9). It remains accepted and held in view; personal accounts are the point of the product.
- The pcapng-inspector project. It is no longer the pilot; it may onboard as a normal project later.
