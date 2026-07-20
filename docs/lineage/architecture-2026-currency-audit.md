# Werft Architecture — 2026/2027 Currency & Completeness Audit

**Subject:** `ARCHITECTURE.md` (entering v1.2; leaving as v1.3)
**Audit date:** 2026-07-20
**Question put to the audit:** *Is the tech stack, logic, and architecture plan truly 2026/2027? Are there new tools, systems, or principles to use? Did we think about everything?*
**Overall verdict:** **Current in doctrine and internals — ahead of the field, in fact — with residual gaps concentrated in the operating envelope and a few stale stack rows. Buildable; revised to v1.3 to close them.**

---

## Method

Multi-agent, web-grounded, adversarially verified — the same discipline as the v1.0→v1.2 passes, pointed outward at the state of the world rather than inward at internal consistency.

- **7 research dimensions**, each web-researched against primary sources (official docs, changelogs, pricing pages, advisories) as of 2026-07-20: base stack, provider CLIs, GitHub platform, sandbox/isolation, Postgres-as-substrate, security landscape, orchestration landscape. (The security dimension was re-run on a higher-capability model after a research-model cyber-topic safeguard aborted the first attempt.)
- **3 adversarial analysis passes** over the research + the full spec: a blind-spot hunter (what did three prior passes miss?), a stack-currency judge (per-row verdicts), and a novelty assessor (adopt / consider / reject against the §14 one-sentence test and the §12 YAGNI discipline).
- **14 fact-checks**: load-bearing external claims extracted from the analyses and adversarially verified (stance: refute). 9 CONFIRMED, 5 partially REFUTED — the corrections are folded into the findings below and into the spec.

No finding was accepted on model memory alone; every external claim carries a primary source in the working notes.

---

## Verdict in one paragraph

The **doctrine and internal machinery are not merely current — the field converged onto Werft's contrarian bets** during 2025–26: CI-as-the-only-merge-gate, LLM review as *advisory only*, ephemeral hardened self-test sandboxes, self-hosting over vendor lock-in, and subscription metering as a structural cost cap. Independent 2026 evidence (GitHub Agent HQ routing all third-party agents through required-status-checks; Jules/Codex/Devin self-testing before PR with LLM review non-gating; a CMU verification-gap benchmark; an OpenHands run that burned ~$4k of tokens and merged nothing; the shutdowns of thin CLI-orchestrator startups) validates these choices more than it threatens them. **Where 2026/2027 reality bites is the operating envelope** the prior passes under-weighted — and the sharpest unhedged risk rhymes with v1: v1 died because it had no trustworthy verifier; v2's provider layer rests on consumer-subscription CLIs whose Terms restrict exactly this usage, whose credentials are whole-account, and whose ban blast-radius escapes the very VM boundary the doctrine promises.

---

## Findings by tier

### Tier 1 — material, bites in month one, changes the spec

1. **Provider ToS + account-ban blast radius escapes the VM.** Driving a consumer subscription headless/unattended is the boundary Anthropic and OpenAI are tightening; a suspension bans Ken's *personal* accounts (outside the VM), and it masquerades as A6, looping him into futile re-logins while the chain degrades to Ollama-only (the net-negative regime v1 names). Anthropic *announced* (≈2026-05-14) moving `claude -p`/Agent SDK off subscription pools to API-rate credits, then *paused* it — the floor under doctrine #3 is already moving. → §13 risk, `policy_block` distinct from `auth_failure`, alert **A8**, dedicated Werft-only accounts (§6.6/§9.5/§13).
2. **Whole-account credential mount reconstitutes the Shai-Hulud / s1ngularity supply-chain trifecta.** The runner runs agent-authored `npm/pip install` with a whole-account provider session mounted; 2025–26 worms specifically harvest "AI-tool configuration files" and invoke installed AI CLIs to hunt secrets. Scoped/short-lived credentials now exist (`claude setup-token`, Console scoped Claude Code key, `apiKeyHelper`, Codex project service-account keys, or a self-hosted LLM gateway). → scoped-credential migration + lockfile-only installs + executed dependency-audit in the oracle (§6.6/§8.6/§13).
3. **DNS exfiltration defeats "fail-closed by topology."** Docker's embedded resolver (127.0.0.11) is present on `internal: true` networks and forwards non-local queries to host upstreams (CVE-2024-29018); the query name itself is an exfil channel squid never sees. → DNS forwarder mitigation, Docker CE ≥26.x floor, softened §6.7 claim, narrowed `*.githubusercontent.com`, squid peek/splice confirmation (§6.7/§10.1).
4. **Sync-back `main → unattended` into a strict-checks branch is directionally inverted.** Strict "require up to date" + "Update branch" merges base into head, so the sync-back either auto-pushes un-promoted `unattended` content onto `main` (violates doctrine #2) or can never land under sustained load (recreates v1's 532-commit drift). → non-strict merge on the sync-back path (§8.4).
5. **"Actions-minutes cost is trivial" is fragile, not false.** At the stated tens/day the overage is ~$9–42/mo (survives), but strict-mode required checks create an O(N²) re-run dynamic and the margin to the danger zone is thin. → reframe from "trivial" to a sized line item + GitHub spending cap; name absent Merge Queue on personal accounts as the driver (§8.2).

### Tier 2 — real, lower blast radius

- **Rocky Linux 9 → 10** (9 exits general support 2027-05-31; verify x86-64-v3 vCPU) — §2.
- **`postgres:18-alpine` volume-path data-loss footgun** (issue #1400; PG is the only stateful service) — §2/§10.1/§10.6.
- **Python 3.13 rationale inverted** (3.13 → security-only ≈Oct 2026) — §2.
- **httpx frozen + governance flag** (last stable Dec 2024; issues closed Feb 2026) on the whole external surface — §2/§13.
- **No dependency/lockfile decision** → uv + `uv.lock` — §2.
- **Adapter correctness:** `codex exec --json` is a JSONL *stream*, not a single object (latent bug); `claude --bare`/`--json-schema`; Kimi → Kimi Code CLI — §6.2/§6.5.
- **Human factors:** parked-run + promotion triage exceeds one operator; no away mode; single-*human* SPOF unnamed — §9.5/§10.5/§13.

### Tier 3 — logic edges beyond the transition-table footnotes

Orphan PRs/branches on cancel/terminal-park (§8.1/§8.5); promotion PR tracks the moving `unattended` head → immutable-SHA manifest (§8.5); Ollama session-cap=1 + NULL `blocked_quota` wake (§4.3/§7); mid-run backlog mutation unspecified (§8.3); code-owner-review-at-0 reliability to verify empirically at onboarding (§8.6).

---

## New tools / principles — adopt / consider / reject

**Adopt:** GitHub Actions supply-chain hardening in onboarding (SHA-pin, minimal `GITHUB_TOKEN`, `actions/checkout` v7, **zizmor**); dispatcher-written **AGENTS.md** + one-line `CLAUDE.md` import; scoped provider credentials; cost circuit-breaker **A8**.

**Consider (with trigger):** Docker **userns-remap** as the cheap "escape ≠ VM root" hardening (higher cost than it looks — bind-mount ownership must account for the subuid offset); Ollama **GPU decision** (CPU-only can't run competitive coding models — budget a GPU or scope routing honestly).

**Reject → §12 ledger:** gVisor/Kata/microVM per-runner isolation (wrong threat model; gVisor conflicts with SELinux-enforcing; Kata needs nested virt a guest VM can't provide); best-of-N parallel dispatch (barred by `ux_runs_one_active_per_item`; quota cost); advisory non-gating LLM review (no consumer on the auto-merge path; one flip from the v1 drift); Agent HQ / Copilot coding agent as subsystem or provider (per-token, GitHub-hosted, second engine); agent-as-CODEOWNERS-approver; mid-run CLI checkpoint/resume; querying undocumented provider quota endpoints; registry-proxy/cooldown infrastructure (superseded by an executed audit gate).

---

## Verification ledger (14 fact-checks)

**Confirmed (9):** Rocky 9/10 support windows; PG18 PGDATA path change (#1400); Python 3.13 → security-only ≈Oct 2026; httpx last-stable-Dec-2024 + issues-closed-Feb-2026; OpenAI acquired Astral (uv/Ruff) Mar 2026; classic branch protection not deprecated + CODEOWNERS-review-at-0 exists on classic *and* rulesets; private-repo Actions minutes (2,000 Free / 3,000 Pro-Team) at $0.006/Linux-min; Docker DNS forwarding CVE-2024-29018; Copilot/Codex/Devin PR review advisory-not-gating.

**Partially refuted (5) — corrections folded in:**
- Anthropic subscription-billing change: announced ≈2026-05-14 effective 2026-06-15 then **paused** — but *not* "the same day" (the pause came later). Substance holds; A8 stands.
- gVisor-no-SELinux **confirmed**; "Kata requires nested virt" is conditional — needed when Kata runs inside a guest VM without direct CPU access, i.e. Werft's own case, which *strengthens* the reject.
- userns-remap keeps the daemon rootful (**confirmed**) but is **not** "workflow-unchanged" — bind mounts break without subuid-offset ownership. Raises the CONSIDER cost.
- SHA-pinning is GitHub's documented "only immutable" method (**confirmed**) but the "following the tj-actions incident" causal framing is not in GitHub's docs — adopt on its own merits.
- A real Feb 2026 CMU paper shows LLM self-selection lagging a pass@K oracle, but the specific "~55% plateau" / "Princeton +2.1pp at 2×" figures don't match primary sources — the reject-best-of-N argument rests on the structural bar + quota, not those numbers.

---

## What changed in v1.3

Applied across §2 (stack currency), §6.2/§6.5/§6.6 (adapters + scoped credentials + lockfile installs + AGENTS.md), §6.7/§10.1 (DNS + Docker CE floor), §4.3/§7 (Ollama session/quota), §8.2/§8.3/§8.4/§8.5/§8.6 (Actions cost, backlog mutation, sync-back fix, promotion SHA, onboarding hardening + orphan cleanup), §9.5/§10.4/§10.5 (A8, ban classification, away mode, throughput realism, GPU decision), §12 (new ledger rejects), §13 (new accepted risks), Appendix A (new tunables), and a new **§15** currency-audit summary. Structural regression locks added to `tests/architecture_spec.test.mjs`.

The one-sentence test (§14) still governs every one of these changes: what merges stays decided exclusively by executed checks, in one engine, with one source of truth, operable by one person.
