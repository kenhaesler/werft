"""The tick/poll engine (SPEC §3.3 items 4-5; SPEC §6.2 poll cadences).

Three independent loops, one process, one `asyncio.TaskGroup` (`run`):

- `tick_once` (default 15 s, SPEC §3.3 item 5: "NOTIFY is latency; the 15 s
  reconciliation tick is correctness") needs no GitHub call at all for its
  first two sweeps and only a manager-permission `RepoOps` for the rest:
  `failed` runs whose `next_attempt_at` has passed re-drive `advance_failed`
  (`finalize.py`) directly, using the latest closed `run_attempts` row's
  outcome (`NULL` if none) — this is the only onward path for the two
  infra-edge `-> failed` writers (`ci_watch.advance_awaiting_ci`,
  `merge_flow._fail_gone`) that CAS a run to `failed` without ever calling
  `advance_failed` themselves; `blocked_quota` runs whose `next_attempt_at`
  has passed wake to `queued` (a plain CAS — the provider's own reported
  reset time was the only fact this ever needed); terminal
  (`canceled`/`merged`) runs carrying a `pr_number` with no `cleanup`
  `run_events` row yet get `cleanup_terminal`'s PR-close/branch-delete
  sweep; every `merging` run gets one more `advance_merging` look, so a
  merge dispatched between two 30 s check polls still lands within the
  tick's own 15 s.
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

`tick_once` and `poll_checks_once` both drive `_advance_all_merging`, and
their 15 s/30 s cadences are coprime enough to coincide: a single
`asyncio.Lock` (`_merging_lock`) held around `_advance_all_merging`'s whole
body — discovery query included — serializes the two, so the same
`merging` row is never handed to two concurrent `squash_merge` calls (the
GitHub mutation happens before any CAS; a second, unguarded caller could
double-merge or leave a merged PR recorded as `awaiting_ci`). A second
scheduler process is out of scope by design (SPEC: one process, one
scheduler) — this lock only needs to cover this one process's two loops.

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

Every sweep also accepts an optional `stop: asyncio.Event`, threaded down
from `run()`'s own stop event, and checks `stop.is_set()` between units
(never mid-unit) before starting the next one — a shutdown mid-sweep stops
picking up new units promptly instead of draining an entire (possibly
large) candidate list first.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from werft.config.settings import Settings
from werft.db.models import Project, Run, RunAttempt, RunEvent
from werft.db.transitions import transition_run
from werft.domain.attempts import AttemptOutcome
from werft.domain.projects import ProjectLifecycle
from werft.domain.runs import RunStatus
from werft.github.ops import RepoOps
from werft.observe.alerts import AlertSink
from werft.orchestrator.backlog import intake, sync_backlog
from werft.orchestrator.ci_watch import advance_awaiting_ci, check_flip
from werft.orchestrator.finalize import QuotaPort, advance_failed
from werft.orchestrator.merge_flow import advance_merging, cleanup_terminal

logger = structlog.get_logger(__name__)

#: `_sweep_terminal_cleanup`'s candidate statuses (decision 1, mirrored from
#: `merge_flow._CLEANUP_STATUSES`): `parked` is deliberately excluded.
_CLEANUP_CANDIDATE_STATUSES = (RunStatus.CANCELED.value, RunStatus.MERGED.value)


class Orchestrator:
    """Owns every session the tick/poll engine ever opens (module
    docstring). `quota` is threaded straight through to `advance_failed`
    by the `failed`-row wake sweep (`_sweep_failed_wake`) — T7's
    dispatch/claim tick is still the only caller that grows it beyond the
    `NullQuota` no-op, but it is no longer unused today.
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
        #: Guards `_advance_all_merging`'s whole body (module docstring):
        #: `tick_once` and `poll_checks_once` both call it on independent
        #: cadences that periodically coincide, and only one of them may
        #: ever be mid-`squash_merge` for a given `merging` row at a time.
        self._merging_lock = asyncio.Lock()

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

    async def _wake_failed_one(self, session: AsyncSession, run_id: Any) -> None:
        """Re-drive a stranded `failed` row through `advance_failed`
        (`finalize.py`) directly — the only onward path for a run CASed to
        `failed` by one of the infra-edge writers (`ci_watch.py`,
        `merge_flow.py`) that never call `advance_failed` themselves. Reads
        the run fresh in this unit's own session (`advance_failed`'s CAS is
        checked against this exact row's version) and the latest closed
        `run_attempts` row's `outcome` — `NULL` when the run never had one
        (e.g. a hard-deadline sweep out of `claimed`) or when the last
        attempt was still `NULL` ("pending oracle") at the moment its run
        was CASed to `failed` out-of-band."""
        run = await session.get(Run, run_id)
        if run is None:
            return
        outcome_value = (
            await session.execute(
                select(RunAttempt.outcome)
                .where(RunAttempt.run_id == run_id)
                .order_by(RunAttempt.attempt_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        outcome = AttemptOutcome(outcome_value) if outcome_value is not None else None
        await advance_failed(session, run, outcome=outcome, exhausted_until=None, quota=self._quota)

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

    async def tick_once(self, stop: asyncio.Event | None = None) -> None:
        await self._sweep_failed_wake(stop)
        await self._sweep_blocked_quota_wake(stop)
        await self._sweep_terminal_cleanup(stop)
        await self._advance_all_merging("tick_merging_advance", stop)

    async def _sweep_failed_wake(self, stop: asyncio.Event | None = None) -> None:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(Run.id).where(
                            Run.status == RunStatus.FAILED.value,
                            Run.next_attempt_at <= func.now(),
                        )
                    )
                )
                .scalars()
                .all()
            )

        for run_id in rows:
            if stop is not None and stop.is_set():
                break
            await self._run_unit(
                "failed_wake",
                run_id,
                lambda session, r=run_id: self._wake_failed_one(session, r),
            )

    async def _sweep_blocked_quota_wake(self, stop: asyncio.Event | None = None) -> None:
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
            if stop is not None and stop.is_set():
                break
            await self._run_unit(
                "blocked_quota_wake",
                run_id,
                lambda session, r=run_id, v=version: self._wake_blocked_quota_one(session, r, v),
            )

    async def _sweep_terminal_cleanup(self, stop: asyncio.Event | None = None) -> None:
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
            if stop is not None and stop.is_set():
                break
            await self._run_unit(
                "terminal_cleanup",
                run_id,
                lambda session, r=run_id, p=project_id: self._cleanup_terminal_one(session, r, p),
            )

    async def _advance_all_merging(self, kind: str, stop: asyncio.Event | None = None) -> None:
        async with self._merging_lock:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        select(Run.id, Run.project_id).where(Run.status == RunStatus.MERGING.value)
                    )
                ).all()

            for run_id, project_id in rows:
                if stop is not None and stop.is_set():
                    break
                await self._run_unit(
                    kind,
                    run_id,
                    lambda session, r=run_id, p=project_id: self._advance_merging_one(
                        session, r, p
                    ),
                )

    # -- poll_issues_once: 60 s backlog sync + intake ------------------------

    async def poll_issues_once(self, stop: asyncio.Event | None = None) -> None:
        async with self._session_factory() as session:
            project_ids = (
                (await session.execute(select(Project.id).where(Project.is_paused.is_(False))))
                .scalars()
                .all()
            )

        for project_id in project_ids:
            if stop is not None and stop.is_set():
                break
            await self._run_unit(
                "poll_issues",
                project_id,
                lambda session, p=project_id: self._poll_issues_one(session, p),
            )

    # -- poll_checks_once: 30 s CI/review/merge polling ----------------------

    async def poll_checks_once(self, stop: asyncio.Event | None = None) -> None:
        await self._poll_awaiting_ci(stop)
        await self._poll_bootstrap_check_flip(stop)
        await self._advance_all_merging("poll_merging_advance", stop)

    async def _poll_awaiting_ci(self, stop: asyncio.Event | None = None) -> None:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Run.id, Run.project_id).where(Run.status == RunStatus.AWAITING_CI.value)
                )
            ).all()

        for run_id, project_id in rows:
            if stop is not None and stop.is_set():
                break
            await self._run_unit(
                "awaiting_ci",
                run_id,
                lambda session, r=run_id, p=project_id: self._advance_awaiting_ci_one(
                    session, r, p
                ),
            )

    async def _poll_bootstrap_check_flip(self, stop: asyncio.Event | None = None) -> None:
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
            if stop is not None and stop.is_set():
                break
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
        self,
        stop: asyncio.Event,
        work: Callable[[asyncio.Event], Awaitable[None]],
        interval_seconds: float,
    ) -> None:
        while not stop.is_set():
            try:
                await work(stop)
            except Exception as exc:  # noqa: BLE001 - one bad tick must not kill this loop
                logger.error("orchestrator.loop_iteration_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
