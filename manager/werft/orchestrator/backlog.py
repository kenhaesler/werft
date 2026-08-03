"""Backlog sync + intake: the seam between GitHub's ready-issue set and
Werft's own `runs` queue (SPEC §6.2 mechanics; SPEC §3.3.6 invariant).

Two pure, short-lived functions, each taking a session and doing exactly one
job:

- `sync_backlog` reconciles `backlog_items` against the currently-labeled
  ready set. SPEC §6.2, verbatim: "Mid-run backlog edits never kill an
  in-flight run; unlabel/close marks the item ineligible for future
  dispatch." That sentence is why this function only ever reads/writes
  `backlog_items` — it never reads or writes `runs`. A run already in
  flight for an item that drops off the ready set (unlabeled, closed, or
  simply not returned this poll) keeps running untouched; the item is just
  marked `is_eligible=False` so a *future* `intake` won't queue a new run
  for it.
- `intake` is the only function that turns eligibility into a run, and only
  INSERTs — it never transitions an existing run (that's
  `db/transitions.py`'s job, driven by the dispatcher, not here). SPEC
  §3.3.6: at most one live run per backlog item, enforced by the partial
  unique index `ux_runs_one_active_per_item` (`WHERE status NOT IN
  ('merged', 'canceled')`). `intake` leans on that same index as the `INSERT
  ... SELECT ... ON CONFLICT DO NOTHING` target rather than re-deriving
  "has a live run" in a subquery: a conflict on this index *is* "already has
  a live run" for that backlog item.
"""

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from werft.db.models import BacklogItem, Project, Run
from werft.github.ops import RepoOps


async def sync_backlog(session: AsyncSession, ops: RepoOps, project: Project) -> None:
    """Fetch `project`'s ready issues and reconcile `backlog_items`.

    A 304 (`ConditionalResult.modified is False`) means GitHub has nothing
    new since the last poll's ETag: zero writes, not even the ineligibility
    sweep below, since "nothing changed" is exactly what a 304 asserts.

    Otherwise: upsert every fetched issue on `(project_id,
    github_issue_number)` — title/body/labels/`github_updated_at`/
    `synced_at` refreshed, `is_eligible` forced back to `True` (an item can
    only reach this branch by still carrying the `werft:ready` label).
    Any row already `is_eligible` in the DB but absent from this fetch
    (unlabeled, closed, or deleted) flips to `is_eligible=False`.
    """
    result = await ops.list_ready_issues()
    if not result.modified:
        return

    # Belt-and-braces: `RepoOps.list_ready_issues` already filters
    # `pull_request` items out of its data (A3). Re-asserting it here means
    # a regression in that upstream filter can never pollute this table.
    issues = [issue for issue in (result.data or []) if "pull_request" not in issue]
    ready_numbers = {issue["number"] for issue in issues}

    if issues:
        insert_stmt = pg_insert(BacklogItem).values(
            [
                {
                    "project_id": project.id,
                    "github_issue_number": issue["number"],
                    "title": issue["title"],
                    "body": issue.get("body") or "",
                    "labels": [label["name"] for label in issue.get("labels", [])],
                    "is_eligible": True,
                    "github_updated_at": datetime.fromisoformat(issue["updated_at"]),
                }
                for issue in issues
            ]
        )
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=[BacklogItem.project_id, BacklogItem.github_issue_number],
            set_={
                "title": insert_stmt.excluded.title,
                "body": insert_stmt.excluded.body,
                "labels": insert_stmt.excluded.labels,
                "is_eligible": True,
                "github_updated_at": insert_stmt.excluded.github_updated_at,
                "synced_at": func.now(),
            },
        )
        await session.execute(upsert_stmt)

    sweep_stmt = (
        update(BacklogItem)
        .where(BacklogItem.project_id == project.id)
        .where(BacklogItem.is_eligible.is_(True))
    )
    if ready_numbers:
        sweep_stmt = sweep_stmt.where(BacklogItem.github_issue_number.notin_(ready_numbers))
    await session.execute(sweep_stmt.values(is_eligible=False))


async def intake(session: AsyncSession, project: Project) -> int:
    """INSERT one `queued` run per eligible backlog item with no live run.

    Paused projects (`project.is_paused`) intake nothing. Returns the number
    of rows Postgres actually inserted — `ON CONFLICT DO NOTHING` skips are
    not counted, so a rerun over the same eligible set returns 0. An item
    whose only run has reached a terminal status (`merged`/`canceled`) sits
    outside the partial index's predicate, so it is not a conflict — this is
    how a fresh `intake` after `merged` creates a new run.
    """
    if project.is_paused:
        return 0

    eligible_items = select(BacklogItem.project_id, BacklogItem.id).where(
        BacklogItem.project_id == project.id,
        BacklogItem.is_eligible.is_(True),
    )
    insert_stmt = (
        pg_insert(Run)
        .from_select(["project_id", "backlog_item_id"], eligible_items)
        .on_conflict_do_nothing()
    )
    result = await session.execute(insert_stmt)
    return result.rowcount
