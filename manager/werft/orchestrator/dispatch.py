"""The claim transaction (SPEC §3.2's `queued -> claimed` edge; SPEC §3.3
items 2 and 3).

One candidate per transaction, in one order, every time:

    advisory lock  ->  account  ->  capacity  ->  candidate row lock
                   ->  config   ->  attempt_no  ->  admission + reservation
                   ->  attempt row  ->  CAS  ->  event

The advisory lock comes **first**, before any row lock, so every claim
transaction queues in the same order and two of them cannot deadlock against
each other. Its key is derived from settings, not from the account row, which
is what lets it be taken before anything is read at all. The candidate is then
taken with `FOR UPDATE ... SKIP LOCKED` in `ix_runs_claimable`'s own
`priority DESC, created_at` order, so a racer never blocks on a row somebody
else is already claiming — it takes the next one. `SKIP LOCKED` keeps two
claimers off the same *row*; the advisory lock keeps them off the same
*account's headroom*. Issue #24's N-racer acceptance fails without both.

The account is resolved before the candidate on purpose: with no account
configured there is nothing to decide about any run, and D4 says a missing
account parks nothing. A run with bad *project* config simply parks on the next
sweep, once an account exists.

"No reservation, no claim" is enforced by construction: the reservation INSERT
and the CAS are the same transaction, and the caller commits both or neither.
This function never commits — `Orchestrator._run_unit` owns the transaction, so
a park, a block and a claim are each all-or-nothing.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

import structlog
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from werft.config.dispatch import DispatchConfig, ProjectDispatch, dispatch_for
from werft.config.settings import Settings
from werft.db.models import BacklogItem, Project, Run, RunAttempt, RunEvent
from werft.db.transitions import transition_run
from werft.domain.attempts import DispatchBehavior
from werft.domain.errors import PermanentError
from werft.domain.runs import ParkedReason, RunStatus, run_branch_name
from werft.observe.alerts import AlertSink
from werft.quota.ledger import LedgerQuota, QuotaRefused
from werft.runner.create_body import ProjectRunnerConfig

logger = structlog.get_logger(__name__)

ClaimStatus = Literal["claimed", "blocked_quota", "parked", "idle", "no_account", "at_capacity"]


def runner_config_for(entry: ProjectDispatch) -> ProjectRunnerConfig:
    """`config` and `runner` are sibling import-linter layers and may not see
    each other's models; this is the one conversion point."""
    return ProjectRunnerConfig(
        image_digest=entry.image_digest,
        memory_bytes=entry.memory_bytes,
        nano_cpus=entry.nano_cpus,
    )


@dataclass(frozen=True)
class ClaimOutcome:
    status: ClaimStatus
    run_id: UUID | None = None
    attempt_no: int | None = None
    detail: str | None = None


async def claim_next(
    session: AsyncSession,
    *,
    quota: LedgerQuota,
    config: DispatchConfig,
    settings: Settings,
    alerts: AlertSink,
    now: datetime,
    live_driver_count: int,
) -> ClaimOutcome:
    account = await quota.lock_and_resolve(session)
    if account is None:
        logger.info("dispatch.no_active_account", provider=settings.quota_provider)
        return ClaimOutcome("no_account")

    # The VM-shaped bound (plan decision D13). Being at capacity is not a
    # verdict on any run: nothing is parked, blocked or reserved.
    #
    # What actually keeps two racers from both passing it is the *dispatcher*,
    # not this lock: `live_driver_count` is counted by `_sweep_dispatch` before
    # this claim transaction opens, and that sweep is sequential and the sole
    # caller — one claim at a time, each recounting from a registry the previous
    # claim has already been added to. A second concurrent caller would need
    # this count moved inside the transaction (or the bound re-asserted under
    # the account lock) to be safe.
    if live_driver_count >= settings.max_concurrent_runs:
        return ClaimOutcome("at_capacity")

    row = (
        await session.execute(
            select(Run, Project, BacklogItem)
            .join(Project, Project.id == Run.project_id)
            .join(BacklogItem, BacklogItem.id == Run.backlog_item_id)
            .where(
                Run.status == RunStatus.QUEUED.value,
                Run.next_attempt_at <= now,
                Project.is_paused.is_(False),
                BacklogItem.is_eligible.is_(True),
            )
            .order_by(Run.priority.desc(), Run.created_at)  # == ix_runs_claimable
            .limit(1)
            .with_for_update(of=Run, skip_locked=True)  # SPEC §3.3 item 2
        )
    ).first()
    if row is None:
        return ClaimOutcome("idle")
    run, project, _item = row

    try:
        entry = dispatch_for(config, project.slug)
    except PermanentError as exc:
        await _park(session, run, project, detail=str(exc), alerts=alerts)
        return ClaimOutcome("parked", run.id, detail=str(exc))

    attempt_no = await quota.next_attempt_no(session, run.id)

    try:
        reservation = await quota.reserve(
            session,
            account=account,
            run_id=run.id,
            attempt_no=attempt_no,
            model=entry.model,
            reservation_seconds=entry.timeout_seconds,
            now=now,
        )
    except QuotaRefused as refusal:
        await _block(session, run, refusal, settings=settings, now=now)
        return ClaimOutcome("blocked_quota", run.id, detail=refusal.reason)

    await session.execute(
        insert(RunAttempt).values(
            run_id=run.id,
            attempt_no=attempt_no,
            provider=account.provider,
            behavior=DispatchBehavior.RETRY.value,
            started_at=now,
        )
    )

    ok = await transition_run(
        session,
        run_id=run.id,
        expected_version=run.version,
        new_status=RunStatus.CLAIMED,
        extra={
            "provider": account.provider,
            "branch_name": run_branch_name(run.id),
            "runner_image_digest": entry.image_digest,
            "lease_expires_at": now + timedelta(seconds=settings.lease_seconds),
            "last_heartbeat_at": now,
            "hard_deadline_at": now
            + timedelta(seconds=entry.timeout_seconds + settings.hard_deadline_grace_seconds),
            # A fresh attempt inherits nothing from the last one.
            "container_id": None,
            "exit_code": None,
            "base_sha": None,
            "error_message": None,
        },
    )
    if not ok:
        # Only reachable if something moved this row out of `queued` between the
        # locking SELECT and here (an out-of-band cancel). The whole unit rolls
        # back — reservation and attempt row with it.
        raise RuntimeError(f"claim_next: lost CAS race on run {run.id} (queued->claimed)")

    session.add(
        RunEvent(
            run_id=run.id,
            event_type="dispatch",
            payload={
                "phase": "claimed",
                "attempt_no": attempt_no,
                "account": account.label,
                "reserved_seconds": reservation.reserved_seconds,
            },
        )
    )
    logger.info(
        "dispatch.claimed",
        run_id=str(run.id),
        project=project.slug,
        attempt_no=attempt_no,
        reserved_seconds=reservation.reserved_seconds,
    )
    return ClaimOutcome("claimed", run.id, attempt_no)


async def _park(
    session: AsyncSession, run: Run, project: Project, *, detail: str, alerts: AlertSink
) -> None:
    ok = await transition_run(
        session,
        run_id=run.id,
        expected_version=run.version,
        new_status=RunStatus.PARKED,
        extra={"parked_reason": ParkedReason.PERMANENT_ERROR.value, "error_message": detail},
    )
    if not ok:
        raise RuntimeError(f"claim_next: lost CAS race on run {run.id} (queued->parked)")
    session.add(
        RunEvent(
            run_id=run.id,
            event_type="dispatch",
            # invariant: `dispatch_for` is the only pre-attempt `PermanentError`
            # source, so this literal is the reason. A second one routed through
            # `_park` must carry the exception's own reason instead.
            payload={"phase": "parked", "reason": "no_dispatch_config", "detail": detail},
        )
    )
    logger.warning("dispatch.parked", run_id=str(run.id), detail=detail)
    await alerts.run_parked(project.slug, run.id, ParkedReason.PERMANENT_ERROR.value)


async def _block(
    session: AsyncSession, run: Run, refusal: QuotaRefused, *, settings: Settings, now: datetime
) -> None:
    """SPEC §3.2's `queued -> blocked_quota`, waking at the *binding* rule's own
    time — floored, so a stale `exhausted_until` in the past cannot spin the
    tick. No alert: the account-level notification is `quota_exhausted_until`,
    fired once where the column is written; alerting here would re-notify every
    15 s (plan decision D14)."""
    retry_at = max(refusal.retry_at, now + timedelta(seconds=settings.blocked_quota_floor_seconds))
    ok = await transition_run(
        session,
        run_id=run.id,
        expected_version=run.version,
        new_status=RunStatus.BLOCKED_QUOTA,
        extra={"next_attempt_at": retry_at},
    )
    if not ok:
        raise RuntimeError(f"claim_next: lost CAS race on run {run.id} (queued->blocked_quota)")
    session.add(
        RunEvent(
            run_id=run.id,
            event_type="dispatch",
            payload={
                "phase": "blocked_quota",
                "reason": refusal.reason,
                "retry_at": retry_at.isoformat(),
            },
        )
    )
    logger.info(
        "dispatch.blocked_quota",
        run_id=str(run.id),
        reason=refusal.reason,
        retry_at=retry_at.isoformat(),
    )
