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
  work, so there is nothing for a backlog sync to feed. It is also the one
  sweep that reads its unit's outcome: the fetch is ETag-conditional and the
  client advances that ETag outside the unit's transaction, so a unit that
  doesn't commit has to retract the advance (`RepoOps.invalidate_conditional`)
  or the next poll takes a free 304 over writes that were rolled back.
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
`asyncio.Lock` (`merging_lock`) held around `_advance_all_merging`'s whole
body — discovery query included — serializes the two, so the same
`merging` row is never handed to two concurrent `squash_merge` calls (the
GitHub mutation happens before any CAS; a second, unguarded caller could
double-merge or leave a merged PR recorded as `awaiting_ci`). A second
scheduler process is out of scope by design (SPEC: one process, one
scheduler) — this lock only needs to cover this one process's loops.

The API layer's accept route is the third caller of `advance_merging` in
this process (its inline post-CAS kick, api/routes.py), and it runs on
this same event loop — so the lock is deliberately **public**: the
composition root publishes this exact instance on `app.state.merging_lock`
and the route takes it around its own kick. A lock the route cannot reach
is a lock that does not close the race it exists to close.

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

The dispatch plane (T7, SPEC §3.2/§4) is folded into that same tick rather
than given a loop of its own (plan decision D13) — a fourth loop would be a
second scheduler in all but name, and a run that starts 15 s later finishes
15 s later out of ninety minutes. It is optional as a whole: without
`DispatchServices` every one of its sweeps returns immediately, which is
exactly the pre-T7 manager. Its order inside `tick_once` is load-bearing:
the two wakes, then lease/deadline/orphan/canceled recovery, then dispatch,
then attend. Wakes before dispatch so a run that just became eligible is
claimable in the same tick; the sweeps before dispatch so headroom a dead
manager was holding is visible to this tick's admission; dispatch before
attend so a run claimed here starts attending immediately.

`_drivers` is the process-local other half of the lease column (`sweeps.py`:
the column is durable, the registry is in-process), and `_sweep_attend` is
the only place an entry is ever created — the same code path adopts a run
claimed 15 s ago and a run this manager inherited from its own crashed
predecessor, so crash recovery is the normal path rather than a second one
that can rot. Driver tasks are deliberately outside `run`'s `TaskGroup`:
they outlive the sweep that spawned them by up to ninety minutes, and at
shutdown they get `drain_drivers`' own bounded budget instead — a cancelled
driver leaves its container running on purpose (D1).

Every sweep also accepts an optional `stop: asyncio.Event`, threaded down
from `run()`'s own stop event, and checks `stop.is_set()` between units
(never mid-unit) before starting the next one — a shutdown mid-sweep stops
picking up new units promptly instead of draining an entire (possibly
large) candidate list first.
"""

import asyncio
import contextlib
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from werft.config.dispatch import DispatchConfigCache
from werft.config.settings import Settings
from werft.db.models import Project, Run, RunAttempt, RunEvent
from werft.db.transitions import transition_run
from werft.domain.attempts import AttemptOutcome
from werft.domain.projects import ProjectLifecycle
from werft.domain.runs import RunStatus
from werft.github.auth import AppAuth
from werft.github.ops import RepoOps
from werft.observe.alerts import AlertSink
from werft.orchestrator.backlog import intake, sync_backlog
from werft.orchestrator.ci_watch import advance_awaiting_ci, check_flip
from werft.orchestrator.dispatch import ClaimOutcome, claim_next
from werft.orchestrator.driver import DriverDeps, attend_run
from werft.orchestrator.finalize import QuotaPort, advance_failed
from werft.orchestrator.merge_flow import advance_merging, cleanup_terminal
from werft.orchestrator.sweeps import (
    SweepDeps,
    sweep_canceled_containers,
    sweep_deadlines,
    sweep_leases,
    sweep_orphan_containers,
)
from werft.providers.base import ProviderSpec
from werft.quota.ledger import LedgerQuota
from werft.runner.docker_api import DockerClient

logger = structlog.get_logger(__name__)

#: `_sweep_terminal_cleanup`'s candidate statuses (decision 1, mirrored from
#: `merge_flow._CLEANUP_STATUSES`): `parked` is deliberately excluded.
_CLEANUP_CANDIDATE_STATUSES = (RunStatus.CANCELED.value, RunStatus.MERGED.value)

#: `_sweep_attend`'s candidate statuses: the two that mean "an attempt is in
#: flight and somebody has to be attending it" (mirrored from `sweeps._IN_FLIGHT`).
_ATTENDABLE_STATUSES = (RunStatus.CLAIMED.value, RunStatus.RUNNING.value)


def _disk_percent_used(path: str) -> float | None:
    """The `artifacts_root` volume's usage as a percentage, or `None` on any
    probe failure (missing/unmounted path, permission error, `total == 0`) —
    `_sweep_disk_threshold` fails open on `None` (plan decision D9): a
    probe that cannot answer must never be the reason claims stop."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return (usage.used / usage.total) * 100.0 if usage.total else None


@dataclass(frozen=True)
class DispatchServices:
    """The collaborators the dispatch plane needs and the pre-T7 tick does not
    (SPEC §4). Optional as a whole, by construction: a manager with no dispatch
    config, no provider credential or no Docker socket still serves `/api/v1`
    and runs its GitHub pollers, and every dispatch sweep is a no-op.

    Deliberately *only* the new pieces: the session factory, `ops_for`, the
    alert sink and the settings the driver and the sweeps also need are already
    the orchestrator's own, and duplicating them here would let a composition
    root hand the dispatch plane a different database than the tick's.
    """

    docker: DockerClient
    auth: AppAuth
    spec: ProviderSpec
    config: DispatchConfigCache
    quota: LedgerQuota


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
        dispatch: DispatchServices | None = None,
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
        #: Public, and published by the composition root on
        #: `app.state.merging_lock`: the accept route's inline
        #: `advance_merging` kick runs on this same event loop and must take
        #: this same instance, or it re-opens the race from the other side.
        self.merging_lock = asyncio.Lock()
        self._dispatch = dispatch
        #: Live attempt drivers, keyed by run id. With one process and one
        #: event loop this dict *is* the in-process fact "somebody is attending
        #: this run", and the sweeps use it alongside the lease column: the
        #: column is the durable half (after a crash this dict is empty and
        #: stale leases are exactly the dead manager's rows), this dict is the
        #: half that protects a live driver whose heartbeat stalled here.
        #: `_sweep_attend` is the ONLY place an entry is ever created
        #: (plan decision D1), which is what makes crash recovery the normal
        #: path rather than a second code path that can rot.
        self._drivers: dict[UUID, asyncio.Task[None]] = {}
        #: `_sweep_disk_threshold`'s rising-edge state (D9): `True` only while
        #: the last probe read at-or-over `disk_threshold_percent`. Starts
        #: `False` so a fresh manager never gates dispatch before its first
        #: tick has actually looked at the disk.
        self._disk_over = False
        #: Built once, not per sweep and not per driver: both are frozen
        #: bundles of collaborators this orchestrator already owns, and
        #: `driver.py`/`sweeps.py` both say the same thing — one instance,
        #: handed to every task.
        self._sweep_bundle = None if dispatch is None else self._build_sweep_deps(dispatch)
        self._driver_bundle = None if dispatch is None else self._build_driver_deps(dispatch)

    @property
    def live_driver_runs(self) -> set[UUID]:
        return set(self._drivers)

    @property
    def live_driver_count(self) -> int:
        return len(self._drivers)

    # -- the per-unit session/transaction wrapper ----------------------------

    async def _run_unit(
        self, kind: str, key: Any, work: Callable[[AsyncSession], Awaitable[None]]
    ) -> bool:
        """Open one session, run `work` inside one transaction, commit on a
        clean return. Any exception rolls back this unit's own transaction
        (never anyone else's — it never had one) and is logged rather than
        raised, so the calling sweep always reaches its next candidate.

        Returns whether the unit committed. Most sweeps ignore that — a
        failed unit just gets retried on the next tick — but a sweep whose
        unit has *non-transactional* side effects (`poll_issues_once`'s ETag
        advance) needs to know, since the rollback cannot undo those for it.
        The `except` deliberately wraps the `async with` as a whole, so a
        failing COMMIT counts as a failed unit exactly like a failing body.
        """
        try:
            async with self._session_factory() as session, session.begin():
                await work(session)
        except Exception as exc:  # noqa: BLE001 - isolate every unit, by design
            logger.error("orchestrator.unit_failed", kind=kind, key=str(key), error=str(exc))
            return False
        return True

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
        was CASed to `failed` out-of-band.

        `advance_failed` never fires `run_parked` itself (`finalize.py`: it
        has no reason to load the project the alert's slug comes from):
        every caller reads the post-advance row and fires it. This sweep is
        a caller,
        and the only onward path for the two infra-edge `-> failed` writers,
        so a park reached this way would otherwise be the one park in the
        system with no operator notification at all."""
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
        await advance_failed(
            session,
            run,
            outcome=outcome,
            exhausted_until=None,
            quota=self._quota,
            alerts=self._alerts,
        )

        advanced = await session.get(Run, run_id, populate_existing=True)
        if advanced.status == RunStatus.PARKED.value:
            project = await session.get(Project, advanced.project_id)
            await self._alerts.run_parked(
                project.slug, advanced.id, advanced.parked_reason or "unknown"
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

    async def _poll_issues_one(
        self, session: AsyncSession, project_id: Any, used_ops: list[RepoOps]
    ) -> None:
        """One project's backlog sync + intake. Appends the `RepoOps` it
        used to `used_ops` *before* making any call with it, so the sweep
        can retract that client's ETag advance if this unit doesn't commit
        (see `poll_issues_once`)."""
        project = await session.get(Project, project_id)
        if project is None:
            return
        ops = self._ops_for(project)
        used_ops.append(ops)
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
        """The 15 s reconciliation tick (SPEC §3.3 item 5: the tick is
        correctness, NOTIFY would only be latency).

        Order is load-bearing (plan decision D13). The disk-threshold probe
        runs first of all (SPEC §8, plan decision D9): it only ever gates
        `_sweep_dispatch`, so nothing else in the tick needs to run before it,
        and putting it first keeps `_disk_over` current for dispatch even if
        an earlier sweep were ever reordered. The wakes run first among the
        rest, so a run that just became eligible is claimable in the *same*
        tick rather than the next one. The sweeps run before dispatch, so
        headroom a dead manager was holding is visible to this tick's
        admission. Dispatch runs before attend, so a run claimed here starts
        attending immediately.
        """
        await self._sweep_disk_threshold(stop)
        await self._sweep_failed_wake(stop)
        await self._sweep_blocked_quota_wake(stop)
        await self._sweep_lease(stop)
        await self._sweep_deadline(stop)
        await self._sweep_orphans(stop)
        await self._sweep_canceled_containers(stop)
        await self._sweep_dispatch(stop)
        await self._sweep_attend(stop)
        await self._sweep_terminal_cleanup(stop)
        await self._advance_all_merging("tick_merging_advance", stop)

    # -- the dispatch plane's sweeps (no-ops without `DispatchServices`) ------

    def _build_sweep_deps(self, dispatch: DispatchServices) -> SweepDeps:
        return SweepDeps(
            session_factory=self._session_factory,
            docker=dispatch.docker,
            quota=dispatch.quota,
            alerts=self._alerts,
            settings=self._settings,
        )

    def _build_driver_deps(self, dispatch: DispatchServices) -> DriverDeps:
        return DriverDeps(
            session_factory=self._session_factory,
            docker=dispatch.docker,
            auth=dispatch.auth,
            ops_for=self._ops_for,
            alerts=self._alerts,
            quota=dispatch.quota,
            spec=dispatch.spec,
            settings=self._settings,
            config=dispatch.config,
        )

    async def _sweep_lease(self, stop: asyncio.Event | None = None) -> None:
        if self._dispatch is None:
            return
        await sweep_leases(
            self._sweep_bundle,
            now=datetime.now(UTC),
            live=self.live_driver_runs,
            stop=stop,
        )

    async def _sweep_deadline(self, stop: asyncio.Event | None = None) -> None:
        if self._dispatch is None:
            return
        await sweep_deadlines(
            self._sweep_bundle,
            now=datetime.now(UTC),
            live=self.live_driver_runs,
            stop=stop,
        )

    async def _sweep_orphans(self, stop: asyncio.Event | None = None) -> None:
        if self._dispatch is None:
            return
        await sweep_orphan_containers(self._sweep_bundle, live=self.live_driver_runs, stop=stop)

    async def _sweep_canceled_containers(self, stop: asyncio.Event | None = None) -> None:
        # D10 puts this one deliberately beyond the registry: the driver that
        # owns a canceled run's container is blocked in `await_completion` and
        # only the die event frees it, so exactly one component owns Docker for
        # a cancel — this sweep. Hence no `live=`.
        if self._dispatch is None:
            return
        await sweep_canceled_containers(self._sweep_bundle, stop=stop)

    async def _sweep_disk_threshold(self, stop: asyncio.Event | None = None) -> None:
        """SPEC §8: past `disk_threshold_percent` usage of `artifacts_root`'s
        volume, stop claiming new runs (plan decision D9). Gate-only — a run
        already `queued` just waits, no state change and no park.

        Fail-open on any probe error: a `None` read means "no evidence of
        trouble", not "assume the worst", so it clears `_disk_over` rather
        than leaving dispatch gated on a probe that cannot even run.

        The alert fires on the rising edge only (`over and not self._disk_over`)
        and re-arms the moment usage drops back under threshold, so an
        operator gets exactly one page per incident rather than one per tick
        for as long as the disk stays full.
        """
        percent = _disk_percent_used(self._settings.artifacts_root)
        if percent is None:
            logger.warning("disk_probe_failed", path=self._settings.artifacts_root)
            self._disk_over = False
            return
        over = percent >= self._settings.disk_threshold_percent
        if over and not self._disk_over:
            await self._alerts.disk_threshold(percent)
        self._disk_over = over

    async def _sweep_dispatch(self, stop: asyncio.Event | None = None) -> None:
        """SPEC §3.2's `queued -> claimed`, folded into the 15 s tick rather
        than given a loop of its own (plan decision D13): a run that starts
        15 s later finishes 15 s later out of ninety minutes, and a fourth loop
        would be a second scheduler in all but name.

        The sweep stops at the first non-`claimed` outcome. The ceiling is
        global, so a claim that does not fit means the next one does not
        either — and continuing would only convert the rest of the queue into
        `blocked_quota` rows the wake sweep has to walk back.

        The config is re-read **once per sweep**, never per candidate: a file
        read has no business on the transaction path, and once per sweep is
        already enough for an image rebuild to take effect without a manager
        restart (D3).

        The concurrency bound is measured by `_live_run_count`, never by the
        registry alone: on the first tick after a restart the registry is empty
        while N in-flight rows are still `claimed`/`running` with valid leases,
        and D13 runs this sweep *before* `_sweep_attend` adopts them — so a
        registry-only budget would admit a fresh batch and then re-adopt the
        old one in the same tick, putting `max_concurrent_runs + N` containers
        on a VM sized for `max_concurrent_runs`.
        """
        if self._dispatch is None:
            return
        if self._disk_over:  # SPEC §8/D9: gate-only, a queued row just waits
            return
        dispatch = self._dispatch
        config = dispatch.config.current()  # last-good on a bad edit (D3)
        budget = min(
            self._settings.dispatch_max_claims_per_tick,
            max(0, self._settings.max_concurrent_runs - await self._live_run_count()),
        )
        for _ in range(budget):
            if stop is not None and stop.is_set():
                return
            sink: list[ClaimOutcome] = []
            # Re-read per candidate: each committed claim adds a `claimed` row,
            # and this is the number `claim_next` re-checks under the advisory
            # lock, where being wrong over-admits.
            live = await self._live_run_count()

            async def unit(
                session: AsyncSession, out: list[ClaimOutcome] = sink, live: int = live
            ) -> None:
                out.append(
                    await claim_next(
                        session,
                        quota=dispatch.quota,
                        config=config,
                        settings=self._settings,
                        alerts=self._alerts,
                        now=datetime.now(UTC),
                        live_driver_count=live,
                    )
                )

            committed = await self._run_unit("dispatch", "claim", unit)
            if not committed or not sink or sink[0].status != "claimed":
                return

    async def _live_run_count(self) -> int:
        """How many runs this VM is really carrying, for the concurrency cap.

        `max(registry, in-flight rows)` rather than either alone. The rows are
        the durable half — after a restart the registry is empty and the
        `claimed`/`running` rows with valid leases are exactly the work this
        process is about to re-adopt, which nothing requeues (the lease sweep
        only acts past `lease_expires_at`). The registry is the in-process
        half — a driver exists for one await before its CAS lands, and for the
        length of its teardown after the row has left `running`.

        `max()` can only over-count, and only transiently, while a finalizing
        row drains: conservative in the direction the cap exists to protect.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(func.count())
                    .select_from(Run)
                    .where(Run.status.in_(_ATTENDABLE_STATUSES))
                )
            ).scalar_one()
        return max(len(self._drivers), rows)

    async def _sweep_attend(self, stop: asyncio.Event | None = None) -> None:
        """Every in-flight run that has no live driver gets one — the same code
        on the first tick after a claim and on the first tick after a restart
        (plan decision D1). The discovery query is the row's own status, so a
        manager that has forgotten everything re-adopts exactly what Postgres
        says is in flight.
        """
        if self._dispatch is None:
            return
        async with self._session_factory() as session:
            run_ids = (
                (await session.execute(select(Run.id).where(Run.status.in_(_ATTENDABLE_STATUSES))))
                .scalars()
                .all()
            )

        for run_id in run_ids:
            if stop is not None and stop.is_set():
                return
            if run_id in self._drivers:
                continue
            self._spawn_driver(run_id)

    def _spawn_driver(self, run_id: UUID) -> None:
        """**The only place a driver is ever created.**

        Registration happens in the same synchronous step as the task's
        creation, before anything can suspend: a driver's first phase is a
        clone that no ticker covers, and a driver invisible to
        `live_driver_runs` for even one await is a driver the lease and
        deadline sweeps — and the concurrency cap — can act against.
        """
        task = asyncio.create_task(attend_run(self._driver_bundle, run_id))
        self._drivers[run_id] = task

        def _done(finished: asyncio.Task[None], rid: UUID = run_id) -> None:
            self._drivers.pop(rid, None)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                # `attend_run` is written never to propagate; if it did, the
                # run is still recoverable from its columns by the sweeps.
                logger.error("orchestrator.driver_failed", run_id=str(rid), error=str(exc))

        task.add_done_callback(_done)

    async def drain_drivers(self) -> None:
        """Wait for live drivers, then cancel the stragglers (plan decision D1).

        Bounded on purpose: a 90-minute run must not hold a SIGTERM open. And a
        cancelled driver does **not** kill its container — the run stays
        `running` in the DB and the next boot's attend sweep re-adopts it.
        Killing a 60-minute agent because the manager restarted is the opposite
        of "state lives in the database". Same discipline as the ntfy drain.
        """
        pending = set(self._drivers.values())
        if not pending:
            return
        _done, still_running = await asyncio.wait(
            pending, timeout=self._settings.driver_drain_seconds
        )
        for task in still_running:
            task.cancel()
        if still_running:
            logger.warning("orchestrator.drivers_cancelled", count=len(still_running))
            await asyncio.gather(*still_running, return_exceptions=True)

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
        async with self.merging_lock:
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
        """Sync + intake for every unpaused project, one unit each.

        This is the one sweep that has to look at its unit's outcome. The
        fetch it drives is ETag-conditional, and `GitHubClient` advances
        that ETag the moment GitHub answers 200 — in memory, in a
        process-lived client, outside the unit's transaction entirely. When
        the unit then rolls back (or its COMMIT fails), the writes derived
        from that body are gone while the ETag advance survives, so the next
        poll would get a free 304, `sync_backlog` would return before
        writing anything, and the labeled issues would never be queued
        again until GitHub's ready set changed for some unrelated reason.
        Retracting the advance on any failed unit is what keeps the two
        commit domains consistent.
        """
        async with self._session_factory() as session:
            project_ids = (
                (await session.execute(select(Project.id).where(Project.is_paused.is_(False))))
                .scalars()
                .all()
            )

        for project_id in project_ids:
            if stop is not None and stop.is_set():
                break
            used_ops: list[RepoOps] = []
            committed = await self._run_unit(
                "poll_issues",
                project_id,
                lambda session, p=project_id, o=used_ops: self._poll_issues_one(session, p, o),
            )
            if not committed:
                for ops in used_ops:
                    ops.invalidate_conditional()

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
        loops have drained (SPEC §3.3: one process, one scheduler).

        The per-run drivers are deliberately **not** in the `TaskGroup`: a
        driver outlives the sweep that spawned it by up to ninety minutes, and
        a group that waited for them would turn shutdown into "wait for the
        agent". They are drained on their own bounded budget instead, after the
        three loops have stopped starting new work (plan decision D1)."""
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._loop(stop, self.tick_once, self._settings.tick_seconds))
            tg.create_task(
                self._loop(stop, self.poll_issues_once, self._settings.issue_poll_seconds)
            )
            tg.create_task(
                self._loop(stop, self.poll_checks_once, self._settings.check_poll_seconds)
            )
        await self.drain_drivers()

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
