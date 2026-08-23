"""Decision 16: classification is a pure function of four structured signals —
the container's exit code, the manager's own ceiling verdict, the parsed
envelope, and whether `result.json` was readable. No I/O, no session, no clock.
"""

import pytest

from werft.contracts.result import ResultStatus, RunResult
from werft.domain.attempts import AttemptOutcome
from werft.orchestrator.driver import classify_completion
from werft.providers.claude import ClaudeSpec
from werft.runner.lifecycle import Completion
from werft.runner.outputs import OutputsRead

SPEC = ClaudeSpec()
GOOD = {"type": "result", "subtype": "success", "is_error": False}
LIMIT = GOOD | {"result": "Claude usage limit reached, resets 2026-08-16T18:00:00+00:00"}


def ok_outputs() -> OutputsRead:
    return OutputsRead(
        result=RunResult(
            status=ResultStatus.SUCCESS,
            started_at="2026-08-16T12:00:00+00:00",
            ended_at="2026-08-16T12:10:00+00:00",
            duration_seconds=600.0,
        ),
        problem=None,
    )


def classify(exit_code, *, timed_out=False, envelope=GOOD, outputs=None, stderr=""):
    return classify_completion(
        spec=SPEC,
        completion=Completion(exit_code=exit_code, timed_out=timed_out),
        outputs=outputs or ok_outputs(),
        envelope=envelope,
        stderr=stderr,
    )


def test_a_clean_exit_with_a_success_envelope_has_no_verdict_yet():
    """Doctrine #1: only an executed check decides whether work is good, so a
    clean attempt with a PR in flight carries `outcome is None`."""
    got = classify(0)
    assert (got.status, got.outcome) == (ResultStatus.SUCCESS, None)


def test_the_manager_ceiling_kill_is_a_timeout_whatever_the_envelope_claims():
    """A killed run may still have written a plausible envelope; the manager's
    own SIGKILL is the fact that wins."""
    got = classify(137, timed_out=True)
    assert got.outcome == AttemptOutcome.TIMEOUT


def test_a_usage_limit_envelope_is_quota_exhausted_with_its_reset_time():
    got = classify(0, envelope=LIMIT)
    assert got.outcome == AttemptOutcome.QUOTA_EXHAUSTED
    assert got.exhausted_until is not None


def test_a_clean_exit_with_an_unreadable_result_json_is_an_agent_failure():
    """SPEC §4.3 makes `result.json` the completion contract: a run that did not
    write one did not complete, whatever its exit code claimed."""
    got = classify(0, outputs=OutputsRead(result=None, problem="invalid_json"))
    assert got.outcome == AttemptOutcome.AGENT_FAILURE
    assert "invalid_json" in got.detail


@pytest.mark.parametrize("exit_code", [2, 4])
def test_the_adapter_contract_tiers_are_infrastructure_facts(exit_code):
    """ "The CLI would not start" / "the workspace was broken" are facts no
    transcript can overrule."""
    got = classify(exit_code, envelope=None, outputs=OutputsRead(result=None, problem="missing"))
    assert got.outcome == AttemptOutcome.INFRA_FAILURE


def test_account_level_stderr_still_wins_on_an_adapter_crash():
    got = classify(
        1,
        envelope=None,
        outputs=OutputsRead(result=None, problem="missing"),
        stderr="Invalid API key · Please run /login",
    )
    assert got.outcome == AttemptOutcome.AUTH_FAILURE


def test_a_serialization_failure_exit_is_the_agents_failure_not_the_boxs():
    """SPEC §4.3's exit 5: the CLI ran, the adapter could not write the
    contract. That is not an infrastructure fact — retrying the same box is
    exactly the right response, but the budget must be spent on it."""
    got = classify(5, envelope=None, outputs=OutputsRead(result=None, problem="missing"))
    assert got.outcome == AttemptOutcome.AGENT_FAILURE


def test_the_ceiling_kill_wins_even_over_the_adapter_exit_tiers():
    got = classify(2, timed_out=True)
    assert got.outcome == AttemptOutcome.TIMEOUT
