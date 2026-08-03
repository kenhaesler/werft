"""The tick/poll engine (SPEC §3.3 items 4-5; SPEC §6.2 poll cadences).

Three independent loops, one process, one `asyncio.TaskGroup` (`run`):

- `tick_once` (default 15 s, SPEC §3.3 item 5: "NOTIFY is latency; the 15 s
  reconciliation tick is correctness") needs no GitHub call at all for its
  first sweep and only a manager-permission `RepoOps` for its other two:
  `blocked_quota` runs whose `next_attempt_at` has passed wake to `queued`
  (a plain CAS — the provider's own reported reset time was the only fact
  this ever needed); terminal (`canceled`/`merged`) runs carrying a
  `pr_number` with no `cleanup` `run_events` row yet get
  `cleanup_terminal`'s PR-close/branch-delete sweep; every `merging` run
  gets one more `advance_merging` look, so a merge dispatched between two
  30 s check polls still lands within the tick's own 15 s.
- `poll_issues_once` (default 60 s, SPEC §6.2) is `sync_backlog` + `intake`
  for every project that isn't paused — a paused project accepts no new
  work, so there is nothing for a backlog sync to feed.
- `poll_checks_once` (default 30 s, SPEC §6.2) is `advance_awaiting_ci` for
  every `awaiting_ci` run, `check_flip` for every `awaiting_review` run
  whose *project* is still `bootstrap` (plan decision 4's observation-site
  guard, enforced here in the poller's own query rather than inside
  `check_flip` itself — an `oracle_gated` project's run never reaches this
  loop at all), and one more `advance_merging` look at every `merging` run
  (so accept-to-merge latency stays inside this poll's own 30 s bound even
  without an inline kick from the operator's own accept call).

Session ownership (SPEC §3.3 item 4, "short-lived `advance()` handlers"):
the orchestrator owns every session. Each unit of work — one project's
backlog sync, one run's CI/merge/flip advance, one run's wake or cleanup —
gets its own session and transaction from `session_factory`, committed (or
rolled back) by `_run_unit` alone; the handler functions themselves
(`sync_backlog`/`intake`/`advance_awaiting_ci`/`advance_merging`/
`check_flip`/`cleanup_terminal`/`transition_run`) never call
`session.commit()`. `_run_unit` also catches every exception a unit raises —
`GitHubUnavailable` and anything else — logs it, and moves on: one
project's GitHub outage, or one run's bug, never starves the rest of that
sweep, and one unit's failure can never roll back another's already-
committed work because the two were never in the same transaction to begin
with. `_loop` adds one more layer of the same belt-and-braces: even a whole
`*_once` sweep raising (e.g. the candidate-discovery query itself hitting a
transient DB error) is caught there too, so a single bad tick never kills
the loop that would have retried it 15/30/60 s later anyway.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from werft.config.settings import Settings
from werft.db.models import Project, Run, RunEvent
from werft.db.transitions import transition_run
from werft.domain.projects import ProjectLifecycle
from werft.domain.runs import RunStatus
from werft.github.ops import RepoOps
from werft.observe.alerts import AlertSink
from werft.orchestrator.backlog import intake, sync_backlog
from werft.orchestrator.ci_watch import advance_awaiting_ci, check_flip
from werft.orchestrator.finalize import QuotaPort
from werft.orchestrator.merge_flow import advance_merging, cleanup_terminal

logger = structlog.get_logger(__name__)

#: `_sweep_terminal_cleanup`'s candidate statuses (decision 1, mirrored from
#: `merge_flow._CLEANUP_STATUSES`): `parked` is deliberately excluded.
_CLEANUP_CANDIDATE_STATUSES = (RunStatus.CANCELED.value, RunStatus.MERGED.value)


class Orchestrator:
    """Owns every session the tick/poll engine ever opens (module
    docstring). `quota` is accepted and stored but unused by any sweep in
    this module today — T7's dispatch/claim tick is the caller that needs
    it; the seam exists now so this constructor's shape does not have to
    change to grow it.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ops_for: Callable[[Project], RepoOps],
        admin_ops_for: Callable[[Project], RepoOps],
        *,
        alerts: AlertSink,
        quota: QuotaPort,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._ops_for = ops_for
        self._admin_ops_for = admin_ops_for
        self._alerts = alerts
        self._quota = quota
        self._settings = settings

    # -- the per-unit session/transaction wrapper ----------------------------

    async def _run_unit(
        self, kind: str, key: Any, work: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        """Open one session, run `work` inside one transaction, commit on a
        clean return. Any exception rolls back this unit's own transaction
        (never anyone else's — it never had one) and is logged rather than
        raised, so the calling sweep always reaches its next candidate."""
        try:
            async with self._session_factory() as session, session.begin():
                await work(session)
        except Exception as exc:  # noqa: BLE001 - isolate every unit, by design
            logger.error("orchestrator.unit_failed", kind=kind, key=str(key), error=str(exc))

    # -- per-row work, one method per handler --------------------------------

    async def _wake_blocked_quota_one(
        self, session: AsyncSession, run_id: Any, version: int
    ) -> None:
        await transition_run(
            session, run_id=run_id, expected_version=version, new_status=RunStatus.QUEUED
        )

    async def _cleanup_terminal_one(
        self, session: AsyncSession, run_id: Any, project_id: Any
    ) -> None:
        run = await session.get(Run, run_id)
        project = await session.get(Project, project_id)
        if run is None or project is None:
            return
        await cleanup_terminal(session, self._ops_for(project), run)

    async def _advance_merging_one(
        self, session: AsyncSession, run_id: Any, project_id: Any
    ) -> None:
        run = await session.get(Run, run_id)
        project = await session.get(Project, project_id)
        if run is None or project is None:
            return
        await advance_merging(session, self._ops_for(project), run, project, alerts=self._alerts)

    async def _poll_issues_one(self, session: AsyncSession, project_id: Any) -> None:
        project = await session.get(Project, project_id)
        if project is None:
            return
        ops = self._ops_for(project)
        await sync_backlog(session, ops, project)
        await intake(session, project)

    async def _advance_awaiting_ci_one(
        self, session: AsyncSession, run_id: Any, project_id: Any
    ) -> None:
        run = await session.get(Run, run_id)
        project = await session.get(Project, project_id)
        if run is None or project is None:
            return
        ops = self._ops_for(project)
        await advance_awaiting_ci(session, ops, run, project, alerts=self._alerts)

    async def _check_flip_one(self, session: AsyncSession, run_id: Any, project_id: Any) -> None:
        run = await session.get(Run, run_id)
        project = await session.get(Project, project_id)
        if run is None or project is None:
            return
        await check_flip(
            session,
            self._ops_for(project),
            self._admin_ops_for(project),
            run,
            project,
            alerts=self._alerts,
        )

    # -- tick_once: the 15 s DB-correctness backstop -------------------------

    async def tick_once(self) -> None:
        await self._sweep_blocked_quota_wake()
        await self._sweep_terminal_cleanup()
        await self._advance_all_merging("tick_merging_advance")

    async def _sweep_blocked_quota_wake(self) -> None:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Run.id, Run.version).where(
                        Run.status == RunStatus.BLOCKED_QUOTA.value,
                        Run.next_attempt_at <= func.now(),
                    )
                )
            ).all()

        for run_id, version in rows:
            await self._run_unit(
                "blocked_quota_wake",
                run_id,
                lambda session, r=run_id, v=version: self._wake_blocked_quota_one(session, r, v),
            )

    async def _sweep_terminal_cleanup(self) -> None:
        cleanup_exists = select(RunEvent.id).where(
            RunEvent.run_id == Run.id, RunEvent.event_type == "cleanup"
        )
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Run.id, Run.project_id).where(
                        Run.status.in_(_CLEANUP_CANDIDATE_STATUSES),
                        Run.pr_number.is_not(None),
                        ~cleanup_exists.exists(),
                    )
                )
            ).all()

        for run_id, project_id in rows:
            await self._run_unit(
                "terminal_cleanup",
                run_id,
                lambda session, r=run_id, p=project_id: self._cleanup_terminal_one(session, r, p),
            )

    async def _advance_all_merging(self, kind: str) -> None:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Run.id, Run.project_id).where(Run.status == RunStatus.MERGING.value)
                )
            ).all()

        for run_id, project_id in rows:
            await self._run_unit(
                kind,
                run_id,
                lambda session, r=run_id, p=project_id: self._advance_merging_one(session, r, p),
            )

    # -- poll_issues_once: 60 s backlog sync + intake ------------------------

    async def poll_issues_once(self) -> None:
        async with self._session_factory() as session:
            project_ids = (
                (await session.execute(select(Project.id).where(Project.is_paused.is_(False))))
                .scalars()
                .all()
            )

        for project_id in project_ids:
            await self._run_unit(
                "poll_issues",
                project_id,
                lambda session, p=project_id: self._poll_issues_one(session, p),
            )

    # -- poll_checks_once: 30 s CI/review/merge polling ----------------------

    async def poll_checks_once(self) -> None:
        await self._poll_awaiting_ci()
        await self._poll_bootstrap_check_flip()
        await self._advance_all_merging("poll_merging_advance")

    async def _poll_awaiting_ci(self) -> None:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Run.id, Run.project_id).where(Run.status == RunStatus.AWAITING_CI.value)
                )
            ).all()

        for run_id, project_id in rows:
            await self._run_unit(
                "awaiting_ci",
                run_id,
                lambda session, r=run_id, p=project_id: self._advance_awaiting_ci_one(
                    session, r, p
                ),
            )

    async def _poll_bootstrap_check_flip(self) -> None:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Run.id, Run.project_id)
                    .join(Project, Project.id == Run.project_id)
                    .where(
                        Run.status == RunStatus.AWAITING_REVIEW.value,
                        Project.lifecycle == ProjectLifecycle.BOOTSTRAP.value,
                    )
                )
            ).all()

        for run_id, project_id in rows:
            await self._run_unit(
                "check_flip",
                run_id,
                lambda session, r=run_id, p=project_id: self._check_flip_one(session, r, p),
            )

    # -- run: three loops, one TaskGroup --------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Run `tick_once`/`poll_issues_once`/`poll_checks_once` on their
        configured cadences until `stop` is set, then return once all three
        loops have drained (SPEC §3.3: one process, one scheduler)."""
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._loop(stop, self.tick_once, self._settings.tick_seconds))
            tg.create_task(
                self._loop(stop, self.poll_issues_once, self._settings.issue_poll_seconds)
            )
            tg.create_task(
                self._loop(stop, self.poll_checks_once, self._settings.check_poll_seconds)
            )

    async def _loop(
        self, stop: asyncio.Event, work: Callable[[], Awaitable[None]], interval_seconds: float
    ) -> None:
        while not stop.is_set():
            try:
                await work()
            except Exception as exc:  # noqa: BLE001 - one bad tick must not kill this loop
                logger.error("orchestrator.loop_iteration_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
