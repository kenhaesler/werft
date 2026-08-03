"""`orchestrator/backlog.py` against a real DB (SPEC §6.2 mechanics; SPEC
§3.3.6 the one-live-run-per-item invariant).

Seeding follows `test_triggers.py`'s style: raw SQL inserts, then a real
`Project`/`BacklogItem`/`Run` row is read back through the ORM so the
functions under test see genuine model instances.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text

from werft.db.models import BacklogItem, Project, Run
from werft.github.client import ConditionalResult
from werft.orchestrator.backlog import intake, sync_backlog


class FakeRepoOps:
    """Duck-typed `RepoOps`: replays one canned `ConditionalResult` per call,
    FIFO — enough for `sync_backlog`, which calls `list_ready_issues` once."""

    def __init__(self, *results: ConditionalResult) -> None:
        self._results = list(results)

    async def list_ready_issues(self) -> ConditionalResult:
        return self._results.pop(0)


def make_issue(
    number: int,
    *,
    title: str = "an issue",
    body: str | None = "body text",
    labels: list[str] = ("werft:ready",),
    updated_at: str = "2026-08-01T12:00:00Z",
    is_pr: bool = False,
) -> dict:
    issue: dict = {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": name} for name in labels],
        "updated_at": updated_at,
    }
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.test/pulls/1"}
    return issue


async def seed_project(session, *, is_paused: bool = False) -> Project:
    tag = uuid.uuid4().hex[:8]
    pid = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo, is_paused) "
                "VALUES (:slug, 'o', :repo, :paused) RETURNING id"
            ),
            {"slug": f"p-{tag}", "repo": f"r-{tag}", "paused": is_paused},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Project, pid)


async def seed_backlog_item(
    session, project: Project, number: int, *, is_eligible: bool = True
) -> BacklogItem:
    bid = (
        await session.execute(
            text(
                "INSERT INTO backlog_items "
                "(project_id, github_issue_number, title, is_eligible, github_updated_at) "
                "VALUES (:p, :n, 't', :e, now()) RETURNING id"
            ),
            {"p": project.id, "n": number, "e": is_eligible},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(BacklogItem, bid)


async def seed_run(session, project: Project, backlog_item: BacklogItem, status: str) -> Run:
    rid = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status) "
                "VALUES (:p, :b, :s) RETURNING id"
            ),
            {"p": project.id, "b": backlog_item.id, "s": status},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Run, rid)


async def advance_run(session, run_id, status: str) -> None:
    await session.execute(
        text("UPDATE runs SET status = :s WHERE id = :id"), {"s": status, "id": run_id}
    )
    await session.commit()


async def backlog_rows(session, project: Project) -> list[BacklogItem]:
    # populate_existing: raw SQL (seed helpers, advance_run) bypasses the ORM,
    # so identity-mapped rows loaded earlier (expire_on_commit=False) must be
    # forced to re-read their attributes here rather than serve stale ones.
    result = await session.execute(
        select(BacklogItem)
        .where(BacklogItem.project_id == project.id)
        .order_by(BacklogItem.github_issue_number)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def run_rows(session, project: Project) -> list[Run]:
    result = await session.execute(
        select(Run).where(Run.project_id == project.id).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


# -- sync_backlog ---------------------------------------------------------


async def test_sync_backlog_upserts_two_ready_issues(db_session) -> None:
    project = await seed_project(db_session)
    ops = FakeRepoOps(
        ConditionalResult(
            modified=True,
            data=[
                make_issue(1, title="first", labels=["werft:ready", "bug"]),
                make_issue(2, title="second"),
            ],
        )
    )
    await sync_backlog(db_session, ops, project)

    rows = await backlog_rows(db_session, project)
    assert [r.github_issue_number for r in rows] == [1, 2]
    assert rows[0].title == "first"
    assert rows[0].labels == ["werft:ready", "bug"]
    assert rows[0].is_eligible is True
    assert rows[1].is_eligible is True


async def test_resync_marks_dropped_item_ineligible_but_survives_mid_run(db_session) -> None:
    project = await seed_project(db_session)
    ops = FakeRepoOps(
        ConditionalResult(
            modified=True,
            data=[make_issue(1, title="stays ready"), make_issue(2, title="will drop")],
        )
    )
    await sync_backlog(db_session, ops, project)
    rows = await backlog_rows(db_session, project)
    dropped_item = rows[1]
    assert dropped_item.github_issue_number == 2

    # A run is already in flight for the item that's about to drop off the
    # ready set — mid-run rule (SPEC §6.2): sync must never touch it.
    run = await seed_run(db_session, project, dropped_item, "queued")
    run_id = run.id
    await advance_run(db_session, run_id, "claimed")
    await advance_run(db_session, run_id, "running")

    ops2 = FakeRepoOps(ConditionalResult(modified=True, data=[make_issue(1, title="stays ready")]))
    await sync_backlog(db_session, ops2, project)

    rows_after = {r.github_issue_number: r for r in await backlog_rows(db_session, project)}
    assert rows_after[1].is_eligible is True
    assert rows_after[2].is_eligible is False

    runs_after = await run_rows(db_session, project)
    assert len(runs_after) == 1
    assert runs_after[0].id == run_id
    assert runs_after[0].status == "running"


async def test_sync_backlog_never_lands_pull_request_items(db_session) -> None:
    """Belt-and-braces: A3's `RepoOps.list_ready_issues` already filters
    `pull_request` items out of its data, but this fake bypasses that (as if
    the upstream filter regressed) to prove A4 asserts the same seam
    independently."""
    project = await seed_project(db_session)
    ops = FakeRepoOps(
        ConditionalResult(
            modified=True,
            data=[
                make_issue(1, title="real issue"),
                make_issue(2, title="disguised pr", is_pr=True),
            ],
        )
    )
    await sync_backlog(db_session, ops, project)

    rows = await backlog_rows(db_session, project)
    assert [r.github_issue_number for r in rows] == [1]


async def test_resync_updates_title_and_body_on_edit(db_session) -> None:
    project = await seed_project(db_session)
    ops1 = FakeRepoOps(
        ConditionalResult(modified=True, data=[make_issue(1, title="old title", body="old body")])
    )
    await sync_backlog(db_session, ops1, project)

    ops2 = FakeRepoOps(
        ConditionalResult(
            modified=True,
            data=[
                make_issue(
                    1,
                    title="new title",
                    body="new body",
                    updated_at="2026-08-02T12:00:00Z",
                )
            ],
        )
    )
    await sync_backlog(db_session, ops2, project)

    rows = await backlog_rows(db_session, project)
    assert len(rows) == 1
    assert rows[0].title == "new title"
    assert rows[0].body == "new body"
    assert rows[0].github_updated_at == datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    # no run was ever created for this item — nothing else to snapshot
    assert await run_rows(db_session, project) == []


async def test_sync_backlog_304_performs_zero_writes(db_session) -> None:
    project = await seed_project(db_session)
    await seed_backlog_item(db_session, project, 1)
    before = await backlog_rows(db_session, project)

    ops = FakeRepoOps(ConditionalResult(modified=False, data=None))
    await sync_backlog(db_session, ops, project)

    after = await backlog_rows(db_session, project)
    assert after == before
    assert after[0].synced_at == before[0].synced_at


# -- intake -----------------------------------------------------------------


async def test_intake_creates_one_run_per_eligible_item_then_zero_on_rerun(db_session) -> None:
    project = await seed_project(db_session)
    await seed_backlog_item(db_session, project, 1)
    await seed_backlog_item(db_session, project, 2)

    inserted = await intake(db_session, project)
    assert inserted == 2

    runs = await run_rows(db_session, project)
    assert len(runs) == 2
    assert {r.status for r in runs} == {"queued"}

    inserted_again = await intake(db_session, project)
    assert inserted_again == 0
    assert len(await run_rows(db_session, project)) == 2


async def test_intake_skips_ineligible_items(db_session) -> None:
    project = await seed_project(db_session)
    await seed_backlog_item(db_session, project, 1, is_eligible=False)

    inserted = await intake(db_session, project)
    assert inserted == 0
    assert await run_rows(db_session, project) == []


async def test_intake_skips_paused_project(db_session) -> None:
    project = await seed_project(db_session, is_paused=True)
    await seed_backlog_item(db_session, project, 1)

    inserted = await intake(db_session, project)
    assert inserted == 0
    assert await run_rows(db_session, project) == []


async def test_intake_creates_fresh_run_after_previous_reaches_merged(db_session) -> None:
    """The partial index's own semantics, in isolation: a `merged` run sits
    outside `ux_runs_one_active_per_item`'s predicate, so it is not a
    conflict and an item that is *still eligible* gets a fresh run.

    This drives the run to `merged` with raw SQL precisely to bypass
    `merge_flow._land_merged`, which in production retires the item
    (`is_eligible=False` + `remove_label`) in the same transaction as the
    merge CAS — that retirement, not this index, is what stops a merged
    milestone re-queuing itself every 60 s (see
    `test_merge_flow.py::test_intake_after_a_merged_run_creates_no_second_run_for_the_same_issue`).
    What this test pins is the requeue path that remains legitimate: an
    operator re-labels a merged item, `sync_backlog` marks it eligible
    again, and intake must then be free to queue follow-up work.
    """
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)

    assert await intake(db_session, project) == 1
    first_run_id = (await run_rows(db_session, project))[0].id

    # drive the run through legal edges to a terminal state
    await advance_run(db_session, first_run_id, "claimed")
    await advance_run(db_session, first_run_id, "running")
    await advance_run(db_session, first_run_id, "awaiting_review")
    await advance_run(db_session, first_run_id, "merging")
    await advance_run(db_session, first_run_id, "merged")

    assert await intake(db_session, project) == 1
    runs = await run_rows(db_session, project)
    assert len(runs) == 2
    statuses = {r.status for r in runs}
    assert statuses == {"merged", "queued"}
    assert all(r.backlog_item_id == item.id for r in runs)
