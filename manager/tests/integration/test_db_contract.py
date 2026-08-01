"""SPEC §3.2: the DB transition table and domain table must be identical."""

from sqlalchemy import text

from werft.domain.runs import TERMINAL_STATUSES, TRANSITIONS, RunStatus


async def test_statuses_identical_to_domain(db_session) -> None:
    rows = (await db_session.execute(text("SELECT status, is_terminal FROM run_statuses"))).all()
    assert {r.status for r in rows} == {s.value for s in RunStatus}
    assert {r.status for r in rows if r.is_terminal} == {s.value for s in TERMINAL_STATUSES}


async def test_transition_table_identical_to_domain(db_session) -> None:
    rows = (
        await db_session.execute(text("SELECT from_status, to_status FROM run_status_transitions"))
    ).all()
    db_pairs = {(RunStatus(r.from_status), RunStatus(r.to_status)) for r in rows}
    assert db_pairs == TRANSITIONS  # exact set equality, both directions
