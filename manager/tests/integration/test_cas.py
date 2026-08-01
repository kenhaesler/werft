"""Every transition is a CAS: WHERE id=:id AND version=:v (SPEC §3.2)."""

from sqlalchemy import text

from werft.db.transitions import transition_run
from werft.domain.runs import RunStatus

from .test_triggers import stage_run


async def test_cas_succeeds_and_bumps_version(db_session) -> None:
    rid = await stage_run(db_session, RunStatus.QUEUED)
    ok = await transition_run(
        db_session, run_id=rid, expected_version=0, new_status=RunStatus.CLAIMED
    )
    await db_session.commit()
    assert ok
    row = (
        await db_session.execute(
            text("SELECT status, version FROM runs WHERE id = :id"), {"id": rid}
        )
    ).one()
    assert row.status == "claimed"
    assert row.version == 1


async def test_cas_noops_on_stale_version(db_session) -> None:
    rid = await stage_run(db_session, RunStatus.QUEUED)
    ok = await transition_run(
        db_session, run_id=rid, expected_version=7, new_status=RunStatus.CLAIMED
    )
    await db_session.rollback()
    assert not ok
    row = (
        await db_session.execute(
            text("SELECT status, version FROM runs WHERE id = :id"), {"id": rid}
        )
    ).one()
    assert row.status == "queued"
    assert row.version == 0
