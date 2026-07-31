# Lineage — the historical record

Everything in this directory is **superseded and frozen**. It is kept because the analysis inside it (adversarial reviews, red-team findings, technology currency research) remains a reference mine during implementation. Nothing here governs the current system — that is `/SPEC.md` and `/README.md`, under the design decisions in `docs/superpowers/specs/2026-07-31-werft-identity-realignment-design.md`. Internal cross-links inside these documents point at their original pre-archive paths and are intentionally not rewritten.

| Artifact | What it was |
|---|---|
| `v1-verdict.md` | Post-mortem of Werft v1 (Claude Agent Station): why LLM-judged verification failed. Still the founding lesson. |
| `architecture-verification.md` | Second independent verification of the v1.2 architecture ("buildable with fixes"). |
| `architecture-2026-currency-audit.md` | The 2026/2027 currency-and-completeness audit that produced architecture v1.3. |
| `ARCHITECTURE-v1.4.md` | The full groundwork specification, v1.4 — 119 KB, four adversarial passes. Superseded by SPEC.md; carries 10 verified defects catalogued in BUILD-PLAN-v1.0.md §14. |
| `BUILD-PLAN-v1.0.md` | Ten-phase build plan derived from v1.4. Its §15 technology-currency research fed SPEC.md's stack pins. Superseded. |
| `product-discovery/core-loop-proof-2026-07-26.md` | Question-at-a-time discovery log with the operator's verbatim answers; quota/evidence/continuity design. |
| `product-discovery/containment-design-2026-07-27.md` | 39-agent containment analysis (16 on design consequences, 23 red-teaming invariant I-1). Its no-root recommendation was overturned by the 2026-07-31 realignment (capable dev boxes); the data-flow controls survive in SPEC.md. |
| `product-discovery/agentic-os-gap-analysis-2026-07-27.md` | Gap analysis: "agentic OS" vs the dispatcher the spec described. Drove the realignment interview. |
| `architecture_spec.test.mjs` | The 28-test regex suite that structurally locked ARCHITECTURE.md v1.4 prose. Frozen with its subject; its file paths are intentionally broken here and it is not meant to run. |
