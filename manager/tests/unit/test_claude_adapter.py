"""Conformance fixtures, one per classification path (issue #21 acceptance).

SPEC §5 is the contract under test. The two highest-value tests here are the
stderr-only ones: an account-level failure filed as a parse error means the
re-auth and quota alerts never fire, which is precisely the invisible-failure
mode they exist for.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from werft.contracts.result import ResultStatus
from werft.contracts.task import TaskSpec
from werft.domain.attempts import AttemptOutcome
from werft.providers.claude import ClaudeSpec, parse_stream

FIXTURES = Path(__file__).parent.parent / "fixtures" / "claude"


def load(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def envelope_from(name: str) -> dict | None:
    return parse_stream(load(name)).result


@pytest.fixture
def spec() -> ClaudeSpec:
    return ClaudeSpec()


@pytest.fixture
def task() -> TaskSpec:
    return TaskSpec(
        run_id="run-1",
        project_slug="elastic",
        provider="claude",
        repo_remote="https://github.com/kenhaesler/elastic.git",
        base_branch="unattended",
        base_sha="a" * 40,
        target_branch="werft/run-1",
        issue_number=7,
        issue_title="Add a parser",
        model="claude-sonnet-5",
        timeout_seconds=5400,
    )


# --- argv and env -------------------------------------------------------------


def test_argv_never_contains_bare(spec, task):
    """SPEC §5: "Never `--bare`" — it is mutually exclusive with the OAuth token
    and discards CLAUDE.md [BP§15.3]."""
    argv = spec.build_argv(
        task, prompt_file="/work/.werft/prompt.md", system_prompt_file="/work/.werft/system.md"
    )
    assert "--bare" not in argv


def test_argv_uses_print_mode_and_stream_json(spec, task):
    argv = spec.build_argv(task, prompt_file="/p", system_prompt_file="/s")
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"


def test_argv_injects_context_via_append_system_prompt_file(spec, task):
    argv = spec.build_argv(task, prompt_file="/p", system_prompt_file="/s")
    assert argv[argv.index("--append-system-prompt-file") + 1] == "/s"


def test_argv_takes_the_model_from_config_never_hardcoded(spec, task):
    """SPEC §5: "this spec hardcodes no model IDs"."""
    argv = spec.build_argv(task, prompt_file="/p", system_prompt_file="/s")
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    other = task.model_copy(update={"model": "claude-opus-5"})
    assert (
        spec.build_argv(other, prompt_file="/p", system_prompt_file="/s")[
            spec.build_argv(other, prompt_file="/p", system_prompt_file="/s").index("--model") + 1
        ]
        == "claude-opus-5"
    )


def test_credential_never_appears_in_argv(spec, task, tmp_path):
    secret = tmp_path / "oauth"
    secret.write_text("sk-ant-oat01-SECRETVALUE\n")
    argv = spec.build_argv(task, prompt_file="/p", system_prompt_file="/s")
    env = spec.build_env(task, credential_path=str(secret))
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-SECRETVALUE"
    assert not any("SECRETVALUE" in part for part in argv), "/proc/*/cmdline is world-readable"


# --- classification: one test per fixture ------------------------------------


def test_success_does_not_assert_a_ci_verdict(spec):
    """Doctrine #1: only executed CI decides whether work is good.

    The CLI finishing cleanly happens before the PR exists and before any check
    has run, so claiming CI_GREEN here would be an LLM-adjacent verdict standing
    in for the oracle. The orchestrator fills the outcome in once CI reports.
    """
    result = spec.classify(envelope=envelope_from("success.jsonl"), stderr="", exit_code=0)
    assert result.status == ResultStatus.SUCCESS
    assert result.outcome is None
    assert result.outcome != AttemptOutcome.CI_GREEN


@pytest.mark.parametrize(
    "text",
    [
        "You have hit your session limit, resets 3:45pm",
        "Claude usage limit reached for this account.",
        "Claude AI usage limit reached|1754073000",
        "Weekly limit reached",
        "You have exceeded your rate limit",
    ],
)
def test_both_word_orders_of_a_limit_stop_are_caught(spec, text):
    """Matching only "hit ... limit" classified "usage limit reached" — the
    phrasing the CLI actually uses — as a SUCCESSFUL run."""
    envelope = {"type": "result", "subtype": "success", "is_error": False, "result": text}
    result = spec.classify(envelope=envelope, stderr="", exit_code=0)
    assert result.outcome == AttemptOutcome.QUOTA_EXHAUSTED, f"missed: {text!r}"
    assert result.status == ResultStatus.QUOTA_EXHAUSTED


def test_an_agents_own_summary_mentioning_limits_is_not_quota_exhaustion(spec):
    """`result` normally holds the agent's summary. A run that genuinely
    succeeded while working ON rate limiting must not park the account."""
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": (
            "Implemented retry handling for the upstream API. Added a test that "
            "asserts we back off when the usage limit is reached, and documented "
            "the rate limit behaviour in the README."
        ),
    }
    result = spec.classify(envelope=envelope, stderr="", exit_code=0)
    assert result.status == ResultStatus.SUCCESS
    assert result.outcome != AttemptOutcome.QUOTA_EXHAUSTED


def test_a_benign_stderr_mentioning_quota_is_not_provider_exhaustion(spec):
    """The bare word "quota" appears in disk-quota warnings and project names."""
    result = spec.classify(
        envelope={"type": "result", "subtype": "success", "is_error": False, "result": "done"},
        stderr="npm warn: disk quota advisory for /home/runner\n",
        exit_code=0,
    )
    assert result.status == ResultStatus.SUCCESS
    assert result.outcome != AttemptOutcome.QUOTA_EXHAUSTED


def test_refusal_is_policy_block(spec):
    result = spec.classify(envelope=envelope_from("refusal.jsonl"), stderr="", exit_code=0)
    assert result.outcome == AttemptOutcome.POLICY_BLOCK
    assert result.status == ResultStatus.POLICY_BLOCK


def test_max_budget_is_agent_failure_not_quota_exhausted(spec):
    """SPEC §5: error_max_budget_usd "is a CLI-side budget, not provider quota".

    Mapping it to quota_exhausted would make it exempt from the retry budget and
    would set exhausted_until on an account that is perfectly healthy.
    """
    result = spec.classify(envelope=envelope_from("max_budget.jsonl"), stderr="", exit_code=1)
    assert result.outcome == AttemptOutcome.AGENT_FAILURE
    assert result.outcome != AttemptOutcome.QUOTA_EXHAUSTED
    assert result.exhausted_until is None


def test_max_turns_is_agent_failure(spec):
    result = spec.classify(envelope=envelope_from("max_turns.jsonl"), stderr="", exit_code=1)
    assert result.outcome == AttemptOutcome.AGENT_FAILURE


def test_error_during_execution_is_agent_failure(spec):
    result = spec.classify(
        envelope=envelope_from("error_during_execution.jsonl"), stderr="", exit_code=1
    )
    assert result.outcome == AttemptOutcome.AGENT_FAILURE
    assert result.status == ResultStatus.FAILURE


def test_hard_usage_limit_text_is_quota_exhausted(spec):
    """No dedicated rate_limit subtype exists in -p output; the hard stop is text."""
    result = spec.classify(envelope=envelope_from("usage_limit_text.jsonl"), stderr="", exit_code=0)
    assert result.outcome == AttemptOutcome.QUOTA_EXHAUSTED
    assert result.status == ResultStatus.QUOTA_EXHAUSTED


def test_reset_time_is_none_when_not_machine_readable(spec):
    """SPEC §5 says "+ reset time when present". A wall-clock string like
    "3:45pm" has no date and no timezone — inventing one would hand §7's ledger
    a wrong number, so it must stay None."""
    result = spec.classify(envelope=envelope_from("usage_limit_text.jsonl"), stderr="", exit_code=0)
    assert result.exhausted_until is None


def test_reset_time_is_parsed_when_iso8601(spec):
    envelope = {
        "type": "result",
        "subtype": "success",
        "result": "You have hit your usage limit, resets 2026-08-01T21:00:00+00:00",
    }
    result = spec.classify(envelope=envelope, stderr="", exit_code=0)
    assert result.outcome == AttemptOutcome.QUOTA_EXHAUSTED
    assert isinstance(result.exhausted_until, datetime)
    assert result.exhausted_until.tzinfo is not None


def test_a_naive_iso_reset_time_is_rejected(spec):
    """exhausted_until is a TIMESTAMPTZ. A value with no offset would be read in
    whatever zone the reader assumes — the same invented number §7 forbids,
    just quieter than a guess."""
    envelope = {
        "type": "result",
        "subtype": "success",
        "result": "You have hit your usage limit, resets 2026-08-01T21:00:00",
    }
    result = spec.classify(envelope=envelope, stderr="", exit_code=0)
    assert result.outcome == AttemptOutcome.QUOTA_EXHAUSTED
    assert result.exhausted_until is None


@pytest.mark.parametrize("exit_code", [143, 137])
def test_both_signal_deaths_are_timeouts(spec, exit_code):
    """The manager's ceiling kills with SIGKILL (137), not SIGTERM (143), so
    handling only 143 left the path that actually fires misclassified."""
    result = spec.classify(envelope=None, stderr="", exit_code=exit_code)
    assert result.outcome == AttemptOutcome.TIMEOUT
    assert result.status == ResultStatus.TIMEOUT


def test_argv_does_not_block_on_permission_prompts(spec, task):
    """An unattended run has nobody to answer a prompt; acceptEdits allows file
    edits only, so every Bash call (git, tests, installs) would hang until the
    ceiling. The container is the wall, not the permission dialog."""
    argv = spec.build_argv(task, prompt_file="/p", system_prompt_file="/s")
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_stderr_pattern_order_is_load_bearing(spec):
    """A suspension message that also says "limit" must classify as policy_block,
    not quota_exhausted: re-auth and quota alerts send the operator different
    places, and a suspended account never recovers by waiting for a reset."""
    result = spec.classify(
        envelope=None,
        stderr="Your account has been suspended for exceeding the usage limit.",
        exit_code=1,
    )
    assert result.outcome == AttemptOutcome.POLICY_BLOCK


def test_stderr_only_auth_failure_is_never_a_parse_error(spec):
    """SPEC §5: account-level failures emit plain stderr with no JSON envelope and
    are "classified by stderr match, never filed as parse errors, or the
    re-auth/quota alerts never fire"."""
    stderr = (FIXTURES / "stderr_auth.txt").read_text(encoding="utf-8")
    result = spec.classify(envelope=None, stderr=stderr, exit_code=1)
    assert result.outcome == AttemptOutcome.AUTH_FAILURE
    assert result.status == ResultStatus.AUTH_FAILURE


def test_stderr_only_suspension_is_policy_block(spec):
    stderr = (FIXTURES / "stderr_suspended.txt").read_text(encoding="utf-8")
    result = spec.classify(envelope=None, stderr=stderr, exit_code=1)
    assert result.outcome == AttemptOutcome.POLICY_BLOCK


def test_stderr_only_usage_limit_is_quota_exhausted(spec):
    result = spec.classify(
        envelope=None, stderr="Claude usage limit reached for this account.", exit_code=1
    )
    assert result.outcome == AttemptOutcome.QUOTA_EXHAUSTED


def test_stderr_wins_even_when_an_envelope_exists(spec):
    """An account-level stderr line is authoritative: a stale or partial envelope
    must not mask a suspension."""
    result = spec.classify(
        envelope={"type": "result", "subtype": "success"},
        stderr="Your account has been suspended.",
        exit_code=1,
    )
    assert result.outcome == AttemptOutcome.POLICY_BLOCK


def test_sigterm_is_a_timeout(spec):
    result = spec.classify(envelope=None, stderr="", exit_code=143)
    assert result.outcome == AttemptOutcome.TIMEOUT
    assert result.status == ResultStatus.TIMEOUT


def test_unparseable_with_no_stderr_match_is_agent_failure_not_silent_success(spec):
    result = spec.classify(envelope=None, stderr="something unexpected", exit_code=1)
    assert result.outcome == AttemptOutcome.AGENT_FAILURE
    assert result.status == ResultStatus.ERROR
    assert result.status != ResultStatus.SUCCESS


# --- stream parsing and usage -------------------------------------------------


def test_parse_stream_finds_the_final_envelope_and_rate_limit_signals(spec):
    parsed = parse_stream(load("api_retry_rate_limit.jsonl"))
    assert parsed.result is not None
    assert len(parsed.rate_limit_signals) == 1, "in-band limit warnings are usage-reader duty (a)"
    assert parsed.rate_limit_signals[0]["error"] == "rate_limit"


def test_transient_api_retry_is_not_a_run_outcome(spec):
    """A retried-and-recovered rate limit must not fail the run."""
    parsed = parse_stream(load("api_retry_rate_limit.jsonl"))
    result = spec.classify(envelope=parsed.result, stderr="", exit_code=0)
    assert result.status == ResultStatus.SUCCESS
    assert result.outcome != AttemptOutcome.QUOTA_EXHAUSTED


def test_parse_stream_survives_a_malformed_line(spec):
    lines = ["not json at all", json.dumps({"type": "result", "subtype": "success"})]
    assert parse_stream(lines).result is not None


def test_read_usage_extracts_the_display_only_numbers(spec):
    usage = spec.read_usage(envelope_from("success.jsonl"))
    assert usage.input_tokens == 1200
    assert usage.output_tokens == 340
    assert usage.total_cost_usd == pytest.approx(0.0271)


def test_read_usage_is_none_without_an_envelope(spec):
    assert spec.read_usage(None) is None
