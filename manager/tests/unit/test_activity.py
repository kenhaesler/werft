from werft.observe.activity import ManagerActivity


def test_activity_records_a_bounded_unit_outcome_for_its_bound_worker() -> None:
    activity = ManagerActivity(available=True)
    worker_token = activity.bind_worker("tick")
    try:
        activity.iteration_started("tick")
        operation_token = activity.unit_started("dispatch", "claim")
        activity.unit_finished(operation_token, kind="dispatch", key="claim", succeeded=False)
    finally:
        activity.reset_worker(worker_token)

    snapshot = activity.snapshot()
    assert snapshot["available"] is True
    assert snapshot["workers"]["tick"]["current_operation"] == {
        "kind": "tick_iteration",
        "key": "",
    }
    assert snapshot["workers"]["tick"]["last_error_at"] is not None
    assert snapshot["recent_operations"][0]["worker"] == "tick"
    assert snapshot["recent_operations"][0]["outcome"] == "failed"
    assert snapshot["recent_operations"][0]["duration_ms"] >= 0


def test_activity_keeps_an_iteration_error_visible_while_waiting_until_recovery() -> None:
    activity = ManagerActivity(available=True)
    worker_token = activity.bind_worker("checks")
    try:
        activity.iteration_started("checks")
        operation_token = activity.unit_started("awaiting_ci", "run-1")
        activity.unit_finished(operation_token, kind="awaiting_ci", key="run-1", succeeded=False)
        activity.iteration_finished("checks")
        activity.waiting("checks", 30)
        failed = activity.snapshot()["workers"]["checks"]

        activity.iteration_started("checks")
        activity.iteration_finished("checks")
        activity.waiting("checks", 30)
        recovered = activity.snapshot()["workers"]["checks"]
    finally:
        activity.reset_worker(worker_token)

    assert failed["state"] == "error"
    assert failed["waiting_until"] is not None
    assert failed["last_error_at"] is not None
    assert recovered["state"] == "waiting"


def test_stopped_activity_cannot_claim_the_manager_is_watching() -> None:
    activity = ManagerActivity(available=True)
    activity.set_live_driver_run_ids(set())
    activity.stop()

    snapshot = activity.snapshot()
    assert snapshot["available"] is False
    assert snapshot["unavailable_reason"] == "stopped"
    assert snapshot["live_driver_run_ids"] == []
    assert all(worker["state"] == "idle" for worker in snapshot["workers"].values())


def test_unavailable_activity_discloses_why_no_manager_exists() -> None:
    snapshot = ManagerActivity.unavailable("github_not_configured").snapshot()
    assert snapshot["available"] is False
    assert snapshot["unavailable_reason"] == "github_not_configured"
    assert snapshot["started_at"] is None
