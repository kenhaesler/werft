"""`observe/alerts.py` (Behavioral decision 9): the protocol shape and the
null implementation's no-op-never-raises contract."""

from datetime import UTC, datetime
from uuid import uuid4

from werft.observe.alerts import AlertSink, NullAlertSink


async def test_null_alert_sink_satisfies_the_protocol() -> None:
    assert isinstance(NullAlertSink(), AlertSink)


async def test_null_alert_sink_review_waiting_is_a_noop() -> None:
    assert (
        await NullAlertSink().review_waiting("acme", uuid4(), "https://example.test/pr/1") is None
    )


async def test_null_alert_sink_run_parked_is_a_noop() -> None:
    assert await NullAlertSink().run_parked("acme", uuid4(), "agent_failure") is None


async def test_null_alert_sink_project_flipped_is_a_noop() -> None:
    assert await NullAlertSink().project_flipped("acme") is None


async def test_null_alert_sink_auth_failure_is_a_noop() -> None:
    assert await NullAlertSink().auth_failure("claude") is None


async def test_null_alert_sink_quota_exhausted_until_is_a_noop() -> None:
    assert await NullAlertSink().quota_exhausted_until("claude", datetime.now(UTC)) is None


async def test_null_alert_sink_disk_threshold_is_a_noop() -> None:
    assert await NullAlertSink().disk_threshold(92.5) is None


def test_ledger_quota_satisfies_the_quota_port_structurally():
    """`werft.quota` may not import `werft.orchestrator`, so nothing but this
    test stands between the two halves of the seam drifting apart."""
    from werft.orchestrator.finalize import QuotaPort
    from werft.quota.ledger import LedgerQuota

    assert isinstance(LedgerQuota(), QuotaPort)


def test_ledger_quota_matches_the_quota_port_signatures():
    """`isinstance` against a `runtime_checkable` Protocol compares *names*
    only: a renamed parameter or a changed return type on either side of the
    seam passes it silently. The signatures are the contract, so they are what
    is asserted. The implementation may add parameters the port does not name,
    but only defaulted ones — a required extra would break every port caller.
    """
    import inspect

    from werft.orchestrator.finalize import QuotaPort
    from werft.quota.ledger import LedgerQuota

    for name in ("release", "next_wake_at"):
        port = inspect.signature(getattr(QuotaPort, name))
        impl = inspect.signature(getattr(LedgerQuota, name))
        assert port.return_annotation == impl.return_annotation, name

        port_params = list(port.parameters.values())
        impl_params = list(impl.parameters.values())
        assert [(p.name, p.annotation, p.kind) for p in impl_params[: len(port_params)]] == [
            (p.name, p.annotation, p.kind) for p in port_params
        ], name
        extra = impl_params[len(port_params) :]
        assert all(p.default is not inspect.Parameter.empty for p in extra), name
