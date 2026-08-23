"""One schema, two consumers, zero drift (SPEC §4.3)."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from werft.contracts.result import ResultError, ResultStatus, RunResult, UsageReport
from werft.contracts.task import TaskSpec
from werft.runner.outputs import read_result


def a_task(**over):
    base = {
        "run_id": "run-1",
        "project_slug": "elastic-log-analysis",
        "provider": "claude",
        "repo_remote": "https://github.com/kenhaesler/elastic.git",
        "base_branch": "unattended",
        "base_sha": "a" * 40,
        "target_branch": "werft/run-1",
        "issue_number": 7,
        "issue_title": "Add a parser",
        "model": "claude-sonnet-5",
        "timeout_seconds": 5400,
    }
    return TaskSpec(**(base | over))


def a_result(**over):
    now = datetime.now(UTC)
    base = {
        "status": ResultStatus.SUCCESS,
        "commit_sha": "b" * 40,
        "pushed": True,
        "started_at": now,
        "ended_at": now,
        "duration_seconds": 12.5,
    }
    return RunResult(**(base | over))


def test_task_round_trips_through_json():
    task = a_task()
    assert TaskSpec.model_validate_json(task.model_dump_json()) == task


def test_result_round_trips_through_json():
    result = a_result(usage=UsageReport(input_tokens=10, output_tokens=20))
    assert RunResult.model_validate_json(result.model_dump_json()) == result


def test_task_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        a_task(sneaky_flag=True)


def test_task_argv_and_env_default_empty():
    task = a_task()
    assert task.argv == []
    assert task.env == {}


def test_task_still_rejects_unknown_keys_after_argv_and_env():
    with pytest.raises(ValidationError):
        a_task(argv=["claude"], env={"CI": "true"}, sneaky_flag=True)


def test_result_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        a_result(privileged=True)


def test_quota_exhausted_is_in_the_status_set_from_day_one():
    """SPEC §4.3 names this explicitly."""
    assert ResultStatus.QUOTA_EXHAUSTED in set(ResultStatus)
    assert a_result(status=ResultStatus.QUOTA_EXHAUSTED).status == "quota_exhausted"


def test_timeout_cannot_exceed_the_ceiling():
    with pytest.raises(ValidationError):
        a_task(timeout_seconds=90 * 60 + 1)


def test_usage_is_display_only_no_admission_fields():
    """SPEC §7: tokens are never admission inputs. The admission dimension is
    metered wall-clock seconds, which deliberately has no field here."""
    fields = set(UsageReport.model_fields)
    assert fields == {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "total_cost_usd",
    }
    assert not any("wallclock" in f or "seconds" in f for f in fields)


def test_read_result_parses_a_valid_document(tmp_path):
    (tmp_path / "result.json").write_text(a_result().model_dump_json())
    read = read_result(str(tmp_path))
    assert read.is_valid
    assert read.result.status == ResultStatus.SUCCESS


def test_read_result_reports_missing(tmp_path):
    read = read_result(str(tmp_path))
    assert not read.is_valid
    assert read.problem == "missing"


def test_read_result_reports_invalid_json(tmp_path):
    (tmp_path / "result.json").write_text("{not json")
    assert read_result(str(tmp_path)).problem == "invalid_json"


def test_read_result_reports_schema_violation(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"status": "made_up"}))
    assert read_result(str(tmp_path)).problem == "schema"


def test_read_result_refuses_an_oversized_document(tmp_path):
    (tmp_path / "result.json").write_text("x" * (2 * 1024 * 1024))
    assert read_result(str(tmp_path)).problem == "too_large"


def test_read_result_refuses_a_symlink(tmp_path):
    """A root agent may replace result.json with a link to a host secret."""
    secret = tmp_path / "secret.pem"
    secret.write_text("KEY")
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    try:
        (outputs / "result.json").symlink_to(secret)
    except OSError:
        pytest.skip("symlink creation not permitted on this platform")
    read = read_result(str(outputs))
    assert not read.is_valid
    assert read.problem == "not_regular"


def test_error_shape_is_structured_not_prose():
    result = a_result(
        status=ResultStatus.ERROR, error=ResultError(code="needs_environment", message="tshark")
    )
    assert result.error.code == "needs_environment"
