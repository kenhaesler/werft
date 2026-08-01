"""The BEFORE UPDATE trigger is the enforcement layer (SPEC §3.2):
illegal transitions are rejected by the database itself, legal ones write
run_events and pg_notify in the same transaction.
"""

import asyncio
import json
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from werft.domain.events import NOTIFY_CHANNEL
from werft.domain.runs import TRANSITIONS, RunStatus


async def stage_run(session, status: RunStatus) -> uuid.UUID:
    tag = uuid.uuid4().hex[:8]
    pid = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo) "
                "VALUES (:slug, 'o', :repo) RETURNING id"
            ),
            {"slug": f"p-{tag}", "repo": f"r-{tag}"},
        )
    ).scalar_one()
    bid = (
        await session.execute(
            text(
                "INSERT INTO backlog_items "
                "(project_id, github_issue_number, title, github_updated_at) "
                "VALUES (:p, 1, 't', now()) RETURNING id"
            ),
            {"p": pid},
        )
    ).scalar_one()
    rid = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status) "
                "VALUES (:p, :b, :s) RETURNING id"
            ),
            {"p": pid, "b": bid, "s": status.value},
        )
    ).scalar_one()
    await session.commit()
    return rid


async def test_every_illegal_transition_rejected_by_db(db_session) -> None:
    """Issue #19 acceptance: the database itself rejects every illegal pair."""
    illegal = [(f, t) for f in RunStatus for t in RunStatus if f != t and (f, t) not in TRANSITIONS]
    assert len(illegal) == 11 * 10 - len(TRANSITIONS)  # 76
    for frm, to in illegal:
        rid = await stage_run(db_session, frm)
        with pytest.raises(DBAPIError, match="illegal run status transition"):
            await db_session.execute(
                text("UPDATE runs SET status = :s WHERE id = :id"),
                {"s": to.value, "id": rid},
            )
        await db_session.rollback()


async def test_every_legal_transition_accepted_and_evented(db_session) -> None:
    for frm, to in sorted(TRANSITIONS):
        rid = await stage_run(db_session, frm)
        await db_session.execute(
            text("UPDATE runs SET status = :s WHERE id = :id"),
            {"s": to.value, "id": rid},
        )
        await db_session.commit()
        evt = (
            await db_session.execute(
                text(
                    "SELECT payload FROM run_events "
                    "WHERE run_id = :id AND event_type = 'status_changed'"
                ),
                {"id": rid},
            )
        ).scalar_one()
        assert evt["from"] == frm.value
        assert evt["to"] == to.value


async def test_created_event_on_insert(db_session) -> None:
    rid = await stage_run(db_session, RunStatus.QUEUED)
    types = (
        (
            await db_session.execute(
                text("SELECT event_type FROM run_events WHERE run_id = :id"), {"id": rid}
            )
        )
        .scalars()
        .all()
    )
    assert "created" in types


async def test_updated_at_stamped_on_transition(db_session) -> None:
    rid = await stage_run(db_session, RunStatus.QUEUED)
    before = (
        await db_session.execute(text("SELECT updated_at FROM runs WHERE id = :id"), {"id": rid})
    ).scalar_one()
    await db_session.execute(text("UPDATE runs SET status = 'claimed' WHERE id = :id"), {"id": rid})
    await db_session.commit()
    after = (
        await db_session.execute(text("SELECT updated_at FROM runs WHERE id = :id"), {"id": rid})
    ).scalar_one()
    # strict: the transition commits in a later transaction than stage_run's
    # INSERT, so an unstamped updated_at would compare equal, not greater
    assert after > before


async def test_event_rolls_back_with_transition(db_session) -> None:
    rid = await stage_run(db_session, RunStatus.QUEUED)
    await db_session.execute(text("UPDATE runs SET status = 'claimed' WHERE id = :id"), {"id": rid})
    await db_session.rollback()  # same-transaction guarantee (SPEC §3.2)
    n = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM run_events "
                "WHERE run_id = :id AND event_type = 'status_changed'"
            ),
            {"id": rid},
        )
    ).scalar_one()
    assert n == 0


async def test_one_active_run_per_backlog_item_enforced(db_session) -> None:
    """SPEC §3.3.6: at most one live run per backlog item (partial unique index)."""
    rid = await stage_run(db_session, RunStatus.QUEUED)
    row = (
        await db_session.execute(
            text("SELECT project_id, backlog_item_id FROM runs WHERE id = :id"), {"id": rid}
        )
    ).one()
    with pytest.raises(IntegrityError, match="ux_runs_one_active_per_item"):
        await db_session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status) VALUES (:p, :b, 'queued')"
            ),
            {"p": row.project_id, "b": row.backlog_item_id},
        )
    await db_session.rollback()
    # a terminal run releases the slot
    await db_session.execute(
        text("UPDATE runs SET status = 'canceled' WHERE id = :id"), {"id": rid}
    )
    await db_session.commit()
    await db_session.execute(
        text("INSERT INTO runs (project_id, backlog_item_id, status) VALUES (:p, :b, 'queued')"),
        {"p": row.project_id, "b": row.backlog_item_id},
    )
    await db_session.commit()


async def test_pr_number_unique_per_project(db_session) -> None:
    """ux_runs_pr: the GitHub-reconciliation idempotency anchor."""
    rid1 = await stage_run(db_session, RunStatus.QUEUED)
    pid = (
        await db_session.execute(text("SELECT project_id FROM runs WHERE id = :id"), {"id": rid1})
    ).scalar_one()
    bid2 = (
        await db_session.execute(
            text(
                "INSERT INTO backlog_items "
                "(project_id, github_issue_number, title, github_updated_at) "
                "VALUES (:p, 2, 't2', now()) RETURNING id"
            ),
            {"p": pid},
        )
    ).scalar_one()
    rid2 = (
        await db_session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status) "
                "VALUES (:p, :b, 'queued') RETURNING id"
            ),
            {"p": pid, "b": bid2},
        )
    ).scalar_one()
    await db_session.execute(text("UPDATE runs SET pr_number = 5 WHERE id = :id"), {"id": rid1})
    await db_session.commit()
    with pytest.raises(IntegrityError, match="ux_runs_pr"):
        await db_session.execute(text("UPDATE runs SET pr_number = 5 WHERE id = :id"), {"id": rid2})
    await db_session.rollback()


async def test_pg_notify_fires_on_transition(db_session, migrated_db) -> None:
    got: asyncio.Queue[str] = asyncio.Queue()
    raw = await asyncpg.connect(migrated_db.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await raw.add_listener(
            NOTIFY_CHANNEL, lambda conn, pid, channel, payload: got.put_nowait(payload)
        )
        rid = await stage_run(db_session, RunStatus.QUEUED)
        await db_session.execute(
            text("UPDATE runs SET status = 'claimed' WHERE id = :id"), {"id": rid}
        )
        await db_session.commit()
        # the insert fires one notify ('created'); the transition fires another
        for _ in range(2):
            payload = json.loads(await asyncio.wait_for(got.get(), timeout=5))
            assert payload == {"t": "run", "id": str(rid)}
    finally:
        await raw.close()
