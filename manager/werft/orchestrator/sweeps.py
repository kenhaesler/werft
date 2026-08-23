"""Crash recovery for the dispatch plane (SPEC §3.2's `claimed -> queued` and
`claimed/running -> failed` edges; SPEC §3.3 items 4 and 5).

These sweeps — not the driver task — are what make "kill -9 the manager"
survivable: every fact they need is a column (`status`, `container_id`,
`lease_expires_at`, `hard_deadline_at`, the open attempt row, the open ledger
row), so a manager that has forgotten everything reconstructs each in-flight
attempt from Postgres alone.

Arbitration with live drivers is one rule, belt and braces (plan decision D12):
**a sweep acts on a row only when its lease has already expired AND its id is
not in the live-driver registry.** The lease column is the durable half — after
a crash the registry is empty and stale leases are exactly the dead manager's
rows. The registry is the in-process half — a driver whose heartbeat stalled
inside this same process must not be raced. A driver that cannot renew its
lease *and* has left the registry is exactly the driver that should lose the
row.

**`sweep_deadlines` is the one deliberate exception: it is gated on the
registry only, never on the lease.** The hard deadline is a ceiling on the
*run*, not on the driver's liveness — a crashed manager can leave a row whose
lease is still valid for up to `lease_seconds` while its deadline has already
blown, and waiting the lease out there just burns the ceiling the deadline
exists to enforce. The `live` check is all the arbitration it needs, because a
driver still in the registry enforces the same ceiling from inside
`RunnerLifecycle`. The two sweeps do not overlap: `sweep_leases` excludes a row
whose deadline has also expired (D12(c), expressed in its query), so a
doubly-expired row is handled exactly once, as a `timeout`.

The container sweeps (`sweep_canceled_containers`, `sweep_orphan_containers`)
are outside the rule entirely by construction: they act on containers belonging
to runs that are no longer in flight at all, and D10 puts the canceled case
deliberately beyond the registry.

Every path here trues quota up in the **same transaction** as its transition
(SPEC §7): `0` when the container never started, observed seconds when it did.
Split them and a crash between the two leaves either a released reservation on
a still-claimed run — which over-admits — or a claimed run whose reservation
nobody will ever close. No path leaks headroom.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from werft.config.settings import Settings
from werft.db.models import Project, Run, RunAttempt, RunEvent
from werft.db.transitions import transition_run
from werft.domain.attempts import AttemptOutcome
from werft.domain.runs import RunStatus
from werft.observe.alerts import AlertSink
from werft.orchestrator.evidence import collect_run_evidence
from werft.orchestrator.finalize import advance_failed
from werft.quota.ledger import LedgerQuota
from werft.runner.docker_api import DockerClient, subnets_of
from werft.runner.workspace import (
    credential_values,
    placement_for,
    remove_secrets,
    scrub_task_json,
)

logger = structlog.get_logger(__name__)

#: The two statuses that mean "an attempt is in flight" — the only ones the
#: lease and deadline rules may act on, and the ones the orphan sweep excludes.
_IN_FLIGHT = (RunStatus.CLAIMED.value, RunStatus.RUNNING.value)

#: The container label `create_body` stamps every run container with; the only
#: durable link from a live container back to a row.
RUN_ID_LABEL = "werft.run_id"

#: The `dispatch` phase that records a *successful* cleanup of one container.
#: `sweep_canceled_containers` is guarded by its absence for the container id on
#: the row (the same `~exists()` shape `loop._sweep_terminal_cleanup` uses), so
#: a second tick is a no-op. `sweep_orphan_containers` needs no such guard: its
#: candidate set is the daemon's own container list, and a container that was
#: really removed stops appearing in it.
_REAPED_PHASE = "reaped"


@dataclass(frozen=True)
class SweepDeps:
    """Everything the crash-recovery path needs, injected. Deliberately smaller
    than `DriverDeps`: a sweep never talks to GitHub and never runs a provider,
    it only settles rows and removes containers."""

    session_factory: async_sessionmaker[AsyncSession]
    docker: DockerClient
    quota: LedgerQuota
    alerts: AlertSink
    settings: Settings


# -- the container half --------------------------------------------------------


async def _container_ids_for(
    deps: SweepDeps, run_id: UUID, container_id: str | None
) -> tuple[list[str], bool]:
    """Every container id that belongs to this run, and whether the scan itself
    succeeded.

    The label is authoritative — a run whose `container_id` never made it to the
    row (crash between `start` and the `claimed -> running` CAS) is still
    findable by it. The row's own id is added anyway: when the daemon cannot be
    listed at all, it is the only handle left, and a `remove` of something
    already gone is a 404 the client swallows.

    A failed scan is reported as a failure rather than as "no containers": the
    caller turns success into a permanent `reaped` marker, and a daemon that
    could not even be listed has proven nothing about what is still running.
    """
    ids: list[str] = []
    scanned = True
    try:
        containers = await deps.docker.list_containers(all_=True)
    except Exception as exc:  # noqa: BLE001 - a daemon outage never blocks a row
        logger.warning("sweep.container_scan_failed", run_id=str(run_id), error=str(exc))
        containers = []
        scanned = False
    for container in containers:
        labels = container.get("Labels") or {}
        found = container.get("Id")
        if labels.get(RUN_ID_LABEL) == str(run_id) and found:
            ids.append(found)
    if container_id and container_id not in ids:
        ids.append(container_id)
    return ids, scanned


async def _capture_network_subnets(deps: SweepDeps, run_id: UUID, network_name: str) -> list[str]:
    """The run network's IPAM subnets, captured **before** this function's
    caller removes it. `[]` on any error — including a daemon outage —
    because the network may legitimately already be gone (a run whose driver
    never got far enough to create one, or a second sweep tick racing the
    first's cleanup); that is routine, never worth more than debug.

    `subnets_of` is inside the `try` on purpose: a malformed IPAM body must
    degrade to `[]` like a daemon outage does, never raise out of a reap that
    still has the network removal and `remove_secrets` left to run."""
    try:
        network = await deps.docker.inspect_network(network_name)
        return subnets_of(network)
    except Exception as exc:  # noqa: BLE001 - the network may already be gone
        logger.debug("sweep.inspect_network_failed", run_id=str(run_id), error=str(exc))
        return []


async def reap_run_containers(deps: SweepDeps, run_id: UUID, container_id: str | None) -> bool:
    """Force-remove every container labelled `werft.run_id=<id>`, remove the
    run's network, scrub the retained `task.json` and delete the mounted secret
    files. Returns **True only when every piece of daemon work succeeded**.

    Best effort, and it **never raises**: a daemon outage must never stop a row
    from being recovered (crash-window row 5). The container is found again on
    the next tick by the orphan sweep; the row cannot wait that patiently,
    because its reservation is open the whole time. But "never raises" is not
    "always worked": the return value is what keeps a transient 500 from being
    recorded as a completed cleanup, which would strand the container and its
    network forever.

    Each container is attempted independently — one unremovable container must
    not skip its siblings — and the network is attempted whatever happened to
    them. `docker_api` already swallows 404/409, so anything that reaches the
    handlers here is a genuine outage.
    """
    placement = placement_for(
        run_id,
        runs_root=deps.settings.runs_root,
        dns_ip=deps.settings.runner_dns_ip,
    )
    subnets = await _capture_network_subnets(deps, run_id, placement.network_name)
    found_ids, ok = await _container_ids_for(deps, run_id, container_id)
    for found in found_ids:
        try:
            await deps.docker.kill_container(found)
            await deps.docker.remove_container(found, force=True)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning(
                "sweep.cleanup_failed", run_id=str(run_id), container_id=found, error=str(exc)
            )
            ok = False
    try:
        await deps.docker.remove_network(placement.network_name)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning(
            "sweep.cleanup_failed",
            run_id=str(run_id),
            network=placement.network_name,
            error=str(exc),
        )
        ok = False
    # Evidence collection precedes the secrets scrub in program order, exactly
    # as the driver's own teardown orders it, but `collect_run_evidence`
    # carries its own D11 wrapper — never raises — so it can never delay or
    # skip the scrub below, whatever `ok` is.
    try:
        await collect_run_evidence(
            deps.session_factory,
            run_id=run_id,
            placement=placement,
            artifacts_root=deps.settings.artifacts_root,
            subnets=subnets,
            squid_access_log=deps.settings.squid_access_log,
            dns_guard_query_log=deps.settings.dns_guard_query_log,
        )
    except Exception as exc:  # noqa: BLE001 - belt and braces around D11's own wrapper
        logger.warning("sweep.evidence_collection_raised", run_id=str(run_id), error=str(exc))
    # Unconditionally, and *after* the daemon work either way: D7's "the
    # retained run dir carries no live credential" is a property of the tree,
    # not of the daemon being reachable, so neither the scrub nor the secret
    # removal is allowed to depend on `ok`. The secret values are read back off
    # `task.json` itself (`workspace.credential_values`) — the driver that built
    # that `env` died with the manager, so no in-memory copy exists to scrub
    # with, and the file is the one thing every path shares. `remove_secrets`
    # is the other half the driver's own teardown does and this path owes:
    # `secrets_dir` lives *inside* the tree SPEC §8 retains and ships offsite.
    # `revoke()` is the one piece impossible here — it needs the in-memory
    # credential object that died with the driver.
    scrub_task_json(placement, secrets=credential_values(placement))
    remove_secrets(placement)
    return ok


# -- shared row helpers --------------------------------------------------------


def _observed_seconds(run: Run, attempt: RunAttempt | None, now: datetime) -> int:
    """Wall clock the provider CLI actually consumed. **Zero whenever the
    container never started** — a CLI that never ran consumed nothing, and
    billing the window for it would over-report headroom loss."""
    if run.container_id is None or attempt is None:
        return 0
    return max(0, int((now - attempt.started_at).total_seconds()))


async def _open_attempt(session: AsyncSession, run_id: UUID) -> RunAttempt | None:
    return (
        await session.execute(
            select(RunAttempt)
            .where(RunAttempt.run_id == run_id, RunAttempt.ended_at.is_(None))
            .order_by(RunAttempt.attempt_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _close_attempt(
    session: AsyncSession,
    attempt: RunAttempt | None,
    *,
    outcome: AttemptOutcome | None,
    observed: int,
    now: datetime,
) -> None:
    """Close the open attempt row. **Closed, never deleted**: `MAX(attempt_no)
    + 1` must stay monotone against both unique keys, and an interrupted
    attempt with no outcome is exactly what `outcome IS NULL` means here."""
    if attempt is None:
        return
    await session.execute(
        update(RunAttempt)
        .where(RunAttempt.id == attempt.id)
        .values(
            outcome=outcome.value if outcome else None,
            ended_at=now,
            duration_seconds=observed,
        )
    )


async def _event(session: AsyncSession, run_id: UUID, phase: str, extra: dict) -> None:
    session.add(RunEvent(run_id=run_id, event_type="dispatch", payload={"phase": phase, **extra}))


async def _alert_if_parked(deps: SweepDeps, session: AsyncSession, run_id: UUID) -> None:
    """SPEC §9: every park site alerts. Read off the post-`advance_failed` row
    here rather than inside it — `run_parked` needs the project slug."""
    advanced = await session.get(Run, run_id, populate_existing=True)
    if advanced.status != RunStatus.PARKED.value:
        return
    project = await session.get(Project, advanced.project_id)
    await deps.alerts.run_parked(project.slug, run_id, advanced.parked_reason or "unknown")


async def _unit(
    deps: SweepDeps, kind: str, run_id: UUID, work: Callable[[AsyncSession], Awaitable[bool]]
) -> bool:
    """One session, one transaction, isolated failure — the same contract
    `Orchestrator._run_unit` gives every other sweep in this process. A unit
    that raises rolls back its own transaction and nobody else's, and the
    calling sweep still reaches its next candidate."""
    try:
        async with deps.session_factory() as session, session.begin():
            return await work(session)
    except Exception as exc:  # noqa: BLE001 - isolate every unit, by design
        logger.error("sweep.unit_failed", kind=kind, run_id=str(run_id), error=str(exc))
        return False


async def _has_reaped_marker(session: AsyncSession, run_id: UUID, container_id: str | None) -> bool:
    """Has *this container* already been reaped successfully?

    Scoped to the container, not to the run: a run is a sequence of attempts and
    each attempt gets its own container, so a run-wide marker would make the
    second attempt's container unreapable forever. The marker is only ever
    written after `reap_run_containers` reported success, so its presence means
    "the daemon work is done", never "we once tried".
    """
    return (
        await session.execute(
            select(RunEvent.id)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "dispatch",
                RunEvent.payload["phase"].astext == _REAPED_PHASE,
                RunEvent.payload["container_id"].astext == container_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


def _stopped(stop: asyncio.Event | None) -> bool:
    return stop is not None and stop.is_set()


# -- the sweeps ----------------------------------------------------------------


async def sweep_deadlines(
    deps: SweepDeps, *, now: datetime, live: set[UUID], stop: asyncio.Event | None = None
) -> int:
    """D12(c): the hard deadline is absolute, and it is checked before the lease
    rule for the same row — a doubly-expired run is a `timeout`, never an
    `infra_failure`. Deliberately **not** gated on lease expiry: the deadline is
    a ceiling on the run, not on the driver's liveness. The live-registry skip
    is the only arbitration it needs, because a driver still in the registry
    enforces this same deadline from inside `RunnerLifecycle`: `_Driver` clamps
    its ceiling to `hard_deadline_at - now`, so a re-adopted run cannot buy
    itself a fresh timeout by outliving the manager that started it.
    """
    async with deps.session_factory() as session:
        rows = (
            await session.execute(
                select(Run.id, Run.container_id).where(
                    Run.status.in_(_IN_FLIGHT),
                    Run.hard_deadline_at.is_not(None),
                    Run.hard_deadline_at < now,
                )
            )
        ).all()

    acted = 0
    for run_id, container_id in rows:
        if _stopped(stop):
            break
        if run_id in live:
            continue
        await reap_run_containers(deps, run_id, container_id)
        if await _unit(
            deps,
            "deadline",
            run_id,
            lambda session, r=run_id, c=container_id: _expire_one(
                deps,
                session,
                r,
                now=now,
                outcome=AttemptOutcome.TIMEOUT,
                phase="deadline_killed",
                extra={"container_id": c},
                detail="hard deadline exceeded",
            ),
        ):
            acted += 1
    return acted


async def sweep_leases(
    deps: SweepDeps, *, now: datetime, live: set[UUID], stop: asyncio.Event | None = None
) -> int:
    """The stale-lease rule. `hard_deadline_at IS NULL OR >= now` is D12(c)'s
    "deadline beats lease for the same row" expressed **in the query** rather
    than in an ordering convention: `tick_once` runs both sweeps in the same
    tick, and a doubly-expired row must be handled exactly once, by the right
    rule, whichever order the two happen to run in.
    """
    async with deps.session_factory() as session:
        rows = (
            await session.execute(
                select(Run.id, Run.container_id).where(
                    Run.status.in_(_IN_FLIGHT),
                    Run.lease_expires_at.is_not(None),
                    Run.lease_expires_at < now,
                    or_(Run.hard_deadline_at.is_(None), Run.hard_deadline_at >= now),
                )
            )
        ).all()

    acted = 0
    for run_id, container_id in rows:
        if _stopped(stop):
            break
        if run_id in live:
            continue
        await reap_run_containers(deps, run_id, container_id)
        if await _unit(
            deps, "lease", run_id, lambda session, r=run_id: _lease_one(deps, session, r, now=now)
        ):
            acted += 1
    return acted


async def _lease_one(
    deps: SweepDeps, session: AsyncSession, run_id: UUID, *, now: datetime
) -> bool:
    """One transaction for one expired lease.

    `claimed` is SPEC §3.2's own "lease expired before container start" edge and
    spends **no budget**: `attempt_count` is untouched, the attempt row is
    closed with no outcome, and the reservation is returned in full. Budget must
    mean N genuine failures, not N interruptions.

    `running` did burn provider wall clock, so it is an `infra_failure` that
    rides `advance_failed`'s ordinary ladder.
    """
    run = (
        await session.execute(select(Run).where(Run.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if run is None or run.status not in _IN_FLIGHT:
        return False  # the row moved between discovery and here

    from_status = run.status
    attempt = await _open_attempt(session, run_id)

    if from_status == RunStatus.CLAIMED.value:
        await _close_attempt(session, attempt, outcome=None, observed=0, now=now)
        await deps.quota.release(session, run, 0)
        ok = await transition_run(
            session,
            run_id=run_id,
            expected_version=run.version,
            new_status=RunStatus.QUEUED,
            extra={
                "next_attempt_at": now,
                "lease_expires_at": None,
                "container_id": None,
            },
        )
        if not ok:
            raise RuntimeError(f"sweep_leases: lost CAS race on run {run_id} (claimed->queued)")
        await _event(session, run_id, "lease_expired", {"from_status": from_status})
        return True

    observed = _observed_seconds(run, attempt, now)
    await _close_attempt(
        session, attempt, outcome=AttemptOutcome.INFRA_FAILURE, observed=observed, now=now
    )
    await deps.quota.release(session, run, observed)
    ok = await transition_run(
        session,
        run_id=run_id,
        expected_version=run.version,
        new_status=RunStatus.FAILED,
        extra={"error_message": "lease expired with no live driver", "lease_expires_at": None},
    )
    if not ok:
        raise RuntimeError(f"sweep_leases: lost CAS race on run {run_id} (running->failed)")
    failed = await session.get(Run, run_id, populate_existing=True)
    await advance_failed(
        session,
        failed,
        outcome=AttemptOutcome.INFRA_FAILURE,
        exhausted_until=None,
        quota=deps.quota,
        alerts=deps.alerts,
    )
    await _event(session, run_id, "lease_expired", {"from_status": from_status})
    await _alert_if_parked(deps, session, run_id)
    return True


async def _expire_one(
    deps: SweepDeps,
    session: AsyncSession,
    run_id: UUID,
    *,
    now: datetime,
    outcome: AttemptOutcome,
    phase: str,
    extra: dict,
    detail: str,
) -> bool:
    """The deadline path: close the attempt, settle the reservation, CAS out of
    `claimed`/`running` and let `advance_failed` route it on. `TIMEOUT` maps to
    `parked_reason='deadline'` in `finalize._PARKED_REASON_BY_OUTCOME`, so a
    wall-clock overrun parks saying so rather than blaming the prompt."""
    run = (
        await session.execute(select(Run).where(Run.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if run is None or run.status not in _IN_FLIGHT:
        return False

    from_status = run.status
    attempt = await _open_attempt(session, run_id)
    observed = _observed_seconds(run, attempt, now)
    await _close_attempt(session, attempt, outcome=outcome, observed=observed, now=now)
    await deps.quota.release(session, run, observed)
    ok = await transition_run(
        session,
        run_id=run_id,
        expected_version=run.version,
        new_status=RunStatus.FAILED,
        extra={"error_message": detail, "lease_expires_at": None},
    )
    if not ok:
        raise RuntimeError(
            f"sweep_deadlines: lost CAS race on run {run_id} ({from_status}->failed)"
        )
    failed = await session.get(Run, run_id, populate_existing=True)
    await advance_failed(
        session,
        failed,
        outcome=outcome,
        exhausted_until=None,
        quota=deps.quota,
        alerts=deps.alerts,
    )
    await _event(session, run_id, phase, {"from_status": from_status, **extra})
    await _alert_if_parked(deps, session, run_id)
    return True


async def sweep_canceled_containers(deps: SweepDeps, *, stop: asyncio.Event | None = None) -> int:
    """D10: a canceled run's container is killed **regardless of the registry**.

    The driver that owns it is blocked in `await_completion`, and only the die
    event frees it — so exactly one component owns Docker for a cancel, and it
    is this sweep. The row keeps its `container_id` after the cancel, so the
    candidate set never empties by itself: the per-container `reaped` marker is
    what keeps this idempotent, and it is written **only** when the reap
    actually succeeded. A daemon outage therefore leaves the run a candidate for
    the next tick instead of retiring it forever.
    """
    async with deps.session_factory() as session:
        rows = (
            await session.execute(
                select(Run.id, Run.container_id).where(
                    Run.status == RunStatus.CANCELED.value,
                    Run.container_id.is_not(None),
                )
            )
        ).all()

    acted = 0
    for run_id, container_id in rows:
        if _stopped(stop):
            break
        async with deps.session_factory() as session:
            if await _has_reaped_marker(session, run_id, container_id):
                continue
        if not await reap_run_containers(deps, run_id, container_id):
            continue
        if await _unit(
            deps,
            "canceled_container",
            run_id,
            lambda session, r=run_id, c=container_id: _mark_reaped(session, r, c),
        ):
            acted += 1
    return acted


async def sweep_orphan_containers(
    deps: SweepDeps, *, live: set[UUID], stop: asyncio.Event | None = None
) -> int:
    """Every labelled container whose run is no longer in flight.

    One mechanism closes two crash windows: "finalize committed, teardown never
    ran" (crash-window row 7) and "cancelled with no live driver". A run still
    in `claimed`/`running`, or one a live driver owns, is left alone — its
    container is not an orphan, it is somebody's work in progress.

    Deliberately **unguarded by the `reaped` marker**. Its candidate set is the
    daemon's own container list, so it is naturally idempotent: a container that
    was really removed stops being listed, and one that is still listed still
    needs removing. A marker here would be actively wrong twice over — it would
    retire a container a transient 500 failed to remove, and it would make the
    *next* attempt's container of a requeued run (`queued`, `failed` and
    `blocked_quota` are all in this sweep's candidate set and all retryable)
    unreapable forever.
    """
    try:
        containers = await deps.docker.list_containers(all_=True)
    except Exception as exc:  # noqa: BLE001 - the rows are recovered without us
        logger.warning("sweep.orphan_scan_failed", error=str(exc))
        return 0

    by_run: dict[str, str | None] = {}
    for container in containers:
        labelled = (container.get("Labels") or {}).get(RUN_ID_LABEL)
        if labelled:
            by_run.setdefault(labelled, container.get("Id"))

    acted = 0
    for labelled, container_id in by_run.items():
        if _stopped(stop):
            break
        try:
            run_id = UUID(labelled)
        except ValueError:
            logger.warning("sweep.orphan_bad_label", label=labelled)
            continue
        if run_id in live:
            continue
        async with deps.session_factory() as session:
            run = await session.get(Run, run_id)
            if run is None or run.status in _IN_FLIGHT:
                continue
        if not await reap_run_containers(deps, run_id, container_id):
            continue
        if await _unit(
            deps,
            "orphan_container",
            run_id,
            lambda session, r=run_id, c=container_id: _mark_reaped(session, r, c),
        ):
            acted += 1
    return acted


async def _mark_reaped(session: AsyncSession, run_id: UUID, container_id: str | None) -> bool:
    """Record that this container was cleaned up. Only ever called after
    `reap_run_containers` returned True: the marker records success, not intent,
    because `sweep_canceled_containers` treats it as final."""
    await _event(session, run_id, _REAPED_PHASE, {"container_id": container_id})
    return True
