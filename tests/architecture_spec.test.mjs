/**
 * Architecture-spec structural tests.
 *
 * Drives the real shipped ARCHITECTURE.md (and README doctrine section).
 * These are document invariants for the groundwork blueprint — not application code.
 *
 * Run: node --test tests/architecture_spec.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const ARCH = join(ROOT, "ARCHITECTURE.md");
const README = join(ROOT, "README.md");
const VERDICT = join(ROOT, "docs", "lineage", "v1-verdict.md");

function load(path) {
  assert.ok(existsSync(path), `missing shipped file: ${path}`);
  return readFileSync(path, "utf8");
}

const arch = load(ARCH);
const readme = load(README);
const verdict = load(VERDICT);

test("ARCHITECTURE.md is the v1.2 groundwork blueprint", () => {
  assert.match(arch, /Groundwork specification, v1\.2 \(2026-07-18\)/);
  assert.match(arch, /doctrine in \[README\.md\]/);
});

test("README doctrine #1–#5 are present and load-bearing", () => {
  assert.match(readme, /Doctrine \(founding decisions\)/);
  for (const n of [1, 2, 3, 4, 5]) {
    assert.match(
      readme,
      new RegExp(`^${n}\\. `, "m"),
      `doctrine #${n} missing from README`,
    );
  }
  assert.match(readme, /Verification is executed, never judged/);
  assert.match(readme, /Blast radius is contained by branch topology/);
  assert.match(readme, /Providers are subscription CLIs/);
  assert.match(readme, /Routing is a static YAML table/);
  assert.match(readme, /The backlog is human-fed only/);
});

test("doctrine #1: merge path has executed CI oracle and no LLM judgment", () => {
  assert.match(arch, /No LLM opinion exists anywhere in this path/);
  assert.match(arch, /werft-oracle/);
  assert.match(arch, /strict_serialized/);
  assert.match(arch, /GitHub Actions/);
  assert.doesNotMatch(
    arch,
    /LLM (merge|verdict|judgment) (gate|decides|approves)/i,
  );
  // YAGNI ledger must ban LLM merge logic
  assert.match(arch, /LLM merge\/conflict\/verdict logic of any kind/);
});

test("doctrine #2: unattended branch + human promotion", () => {
  assert.match(arch, /unattended/);
  assert.match(arch, /### 8\.5 Promotion/);
  assert.match(arch, /Werft may nudge.*never auto-promotes/s);
});

test("doctrine #3: process-layer providers + quota not tokens", () => {
  assert.match(arch, /claude.*codex.*kimi.*ollama/i);
  assert.match(arch, /rolling_window_hours/);
  assert.match(arch, /never.*feed any decision|never a quota input/i);
  assert.match(arch, /Observational token counts.*never.*decision/s);
});

test("doctrine #4: static routing.yaml + outcome recording, no learned router", () => {
  assert.match(arch, /routing\.yaml/);
  assert.match(arch, /decide_dispatch/);
  assert.match(arch, /run_attempts/);
  assert.match(arch, /no learned router/i);
  assert.match(arch, /import-linter/);
});

test("doctrine #5: human-fed backlog only, DB-enforced", () => {
  assert.match(arch, /werft:ready/);
  assert.match(arch, /\*\*only\*\* intake path/);
  assert.match(arch, /backlog_item_id\s+UUID NOT NULL/);
  assert.match(arch, /agent-generated backlog/);
});

test("run state machine lists all 10 statuses and terminal set", () => {
  const statuses = [
    "queued",
    "claimed",
    "running",
    "awaiting_ci",
    "merging",
    "blocked_quota",
    "failed",
    "parked",
    "merged",
    "canceled",
  ];
  for (const s of statuses) {
    assert.match(arch, new RegExp(`\\b${s}\\b`), `status ${s} missing`);
  }
  assert.match(arch, /Terminal:.*`merged`.*`canceled` only/);
  assert.match(arch, /Complete legal-transition table/);
});

test("legal-transition table has a row for every non-terminal status", () => {
  const fromStates = [
    "queued",
    "claimed",
    "running",
    "awaiting_ci",
    "merging",
    "blocked_quota",
    "failed",
    "parked",
  ];
  for (const s of fromStates) {
    assert.match(
      arch,
      new RegExp(`\\| ${s} \\|`),
      `transition table missing from-row for ${s}`,
    );
  }
});

test("claim+quota atomicity and one-writer contracts are specified", () => {
  assert.match(arch, /pg_advisory_xact_lock/);
  assert.match(arch, /same transaction as the queued→claimed CAS|same transaction as the claim/i);
  assert.match(arch, /One writer/i);
  assert.match(arch, /Runners are DB-blind/);
  assert.match(arch, /nothing but the manager process ever holds a Postgres connection/);
});

test("docker completion is event-driven (sanctioned long-lived task)", () => {
  assert.match(arch, /event-driven, never a per-run blocking call/);
  assert.match(arch, /Docker events stream|GET \/events/);
  assert.match(arch, /docker inspect/);
});

test("git-token re-mint covers 90 min ceiling vs 1 h TTL", () => {
  assert.match(arch, /1 h TTL|1 h token/);
  assert.match(arch, /90 min/);
  assert.match(arch, /45 min/);
});

test("compose topology gives manager and runners egress via proxy", () => {
  assert.match(arch, /mgr_egress/);
  assert.match(arch, /runner_net/);
  assert.match(arch, /HTTPS_PROXY/);
  assert.match(arch, /egress-proxy/);
});

test("oracle mutability residual is documented (accepted risk #8)", () => {
  assert.match(arch, /touches_tests/);
  assert.match(arch, /The oracle's test suite is agent-editable/);
  assert.match(arch, /reward-hacking vector/);
  assert.match(arch, /oracle_attested|human checklist/i);
});

test("v1-verdict failure modes have lineage source on disk", () => {
  assert.match(verdict, /judgment is not verification/i);
  assert.match(verdict, /executes nothing/i);
  assert.match(verdict, /reward-hack/i);
});

/**
 * Footnote ² prose (shipped) routes CI timeout to park edges, not agent hard-deadline.
 */
test("footnote 2 prose routes CI timeout to park edges, not agent hard-deadline", () => {
  assert.match(arch, /WERFT_CI_WAIT_TIMEOUT/);
  assert.match(
    arch,
    /`awaiting_ci`\/`merging` are governed by the separate `WERFT_CI_WAIT_TIMEOUT`/,
  );
  assert.match(arch, /expiry parks via the existing/);
  assert.match(
    arch,
    /bounds \*\*agent execution only\*\* \(`claimed, running`\)/,
  );
});

/**
 * Regression locks for the two majors from architecture-verification.md,
 * inverted from characterization trackers after the v1.2 fixes.
 */
test("M2 closed: §6.4 completion authority is the die-event/inspect mechanism, not docker wait", () => {
  assert.doesNotMatch(
    arch,
    /completion authority is always `docker wait`/,
    "M2 regressed: §6.4 names blocking docker wait as completion authority again",
  );
  assert.match(arch, /completion authority is always the container's \*\*`die` event/);
  assert.match(arch, /event-driven, never a per-run blocking call/);
});

test("M1 closed: awaiting_ci/merging failed-cells carry footnote 10, and footnote 2 stays scoped", () => {
  const lines = arch.split(/\r?\n/);
  const awaiting = lines.find((l) => l.startsWith("| awaiting_ci |"));
  const merging = lines.find((l) => l.startsWith("| merging |"));
  assert.ok(awaiting, "awaiting_ci transition row missing");
  assert.ok(merging, "merging transition row missing");
  assert.doesNotMatch(awaiting, /✓²(?!⁰)/u, `M1 regressed — footnote ² back on awaiting_ci: ${awaiting}`);
  assert.doesNotMatch(merging, /✓²(?!⁰)/u, `M1 regressed — footnote ² back on merging: ${merging}`);
  assert.match(awaiting, /✓¹⁰/, `awaiting_ci → failed should carry footnote ¹⁰: ${awaiting}`);
  assert.match(merging, /✓¹⁰/, `merging → failed should carry footnote ¹⁰: ${merging}`);
  assert.match(arch, /¹⁰ unrecoverable error observed \*\*during\*\* CI-wait\/merge/);
});
