"""The quota ledger: SPEC §7's self-cap, as the port dispatch and finalize call.

Everything here runs inside a transaction its caller opened and will commit —
"every quota mutation in the same transaction as the state transition it
accompanies" is not a convention in this module, it is the reason it contains
no `commit()` anywhere.

Layering (import-linter): `werft.quota` sits below `werft.orchestrator` and may
never import it, so `LedgerQuota` satisfies `orchestrator.finalize.QuotaPort`
**structurally**. A test asserts the match rather than a base class enforcing
it.

`true_up` is a *guarded* update (`WHERE ... AND actual_wallclock_s IS NULL`).
Two paths can legitimately settle the same attempt — an operator cancel and the
driver that later notices the container died — and the guard is what makes the
second one a no-op instead of a rewrite of an already-settled number. First
writer wins.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from werft.db.models import ProviderAccount, QuotaLedgerEntry, Run, RunAttempt
from werft.quota.accounts import lock_account_key, resolve_account
from werft.quota.admission import AccountLimits, Admission, decide
from werft.quota.window import earliest_headroom_at, read_closed_in_window, read_window

logger = structlog.get_logger(__name__)

#: SPEC §5 documents no reset field in the common case; this is the same 15
#: minutes `advance_failed` has always used for its own retry heuristic.
#: Recorded as an invented number, and distinguishable in `exhausted_source`.
EXHAUSTED_FALLBACK_MINUTES = 15


@dataclass(frozen=True)
class Reservation:
    account_id: UUID
    provider: str
    reserved_seconds: int


class QuotaRefused(Exception):
    """Admission said no. `retry_at` is when the *binding* rule stops binding."""

    def __init__(self, reason: str, retry_at: datetime) -> None:
        super().__init__(f"quota refused: {reason}")
        self.reason = reason
        self.retry_at = retry_at


class LedgerQuota:
    def __init__(
        self,
        *,
        provider: str = "claude",
        label: str = "primary",
        fallback_seconds: int = 900,
        typical_reservation_seconds: int = 5400,
    ) -> None:
        self._provider = provider
        self._label = label
        self._fallback_seconds = fallback_seconds
        self._typical = typical_reservation_seconds

    # -- the claim path -----------------------------------------------------

    async def lock_and_resolve(self, session: AsyncSession) -> ProviderAccount | None:
        """Take the per-account advisory lock, *then* read the account row.

        The lock is keyed on provider/label (settings), not on the row's id, so
        it can be taken before anything is read — which is what makes it the
        first lock every claim transaction acquires (plan decision D2). `None`
        means no active account is configured: a system-wide misconfiguration
        the sweep logs and skips, never a per-run verdict (D4).
        """
        await lock_account_key(session, self._provider, self._label)
        return await resolve_account(session, provider=self._provider, label=self._label)

    async def admit(
        self,
        session: AsyncSession,
        account: ProviderAccount,
        *,
        reservation_seconds: int,
        now: datetime,
    ) -> Admission:
        usage = await read_window(session, account.id, now=now)
        closed = await read_closed_in_window(
            session, account.id, now=now, window_hours=account.rolling_window_hours
        )
        verdict = decide(
            _limits_of(account),
            usage,
            closed,
            reservation_seconds=reservation_seconds,
            now=now,
            fallback_seconds=self._fallback_seconds,
        )
        if not verdict.ok:
            logger.info(
                "quota.admission_denied",
                account=str(account.id),
                reason=verdict.reason,
                consumed=verdict.effective_consumed_seconds,
                reserved=usage.reserved_seconds,
                reservation=reservation_seconds,
                ceiling=account.ceiling_seconds,
            )
        elif verdict.effective_consumed_seconds > usage.consumed_seconds:
            logger.info(
                "quota.provider_tightened",
                account=str(account.id),
                ledger_consumed=usage.consumed_seconds,
                effective_consumed=verdict.effective_consumed_seconds,
            )
        return verdict

    async def reserve(
        self,
        session: AsyncSession,
        *,
        account: ProviderAccount,
        run_id: UUID,
        attempt_no: int,
        model: str | None,
        reservation_seconds: int,
        now: datetime,
    ) -> Reservation:
        verdict = await self.admit(
            session, account, reservation_seconds=reservation_seconds, now=now
        )
        if not verdict.ok:
            raise QuotaRefused(
                verdict.reason,
                verdict.retry_at or now + timedelta(seconds=self._fallback_seconds),
            )
        await session.execute(
            insert(QuotaLedgerEntry).values(
                provider_account_id=account.id,
                run_id=run_id,
                attempt_no=attempt_no,
                model=model,
                reserved_wallclock_s=reservation_seconds,
                # Explicit, never the column default: the window is only
                # testable on a synthetic clock if this is a parameter.
                consumed_at=now,
            )
        )
        return Reservation(account.id, account.provider, reservation_seconds)

    async def next_attempt_no(self, session: AsyncSession, run_id: UUID) -> int:
        """`1 + max(max(run_attempts.attempt_no), max(quota_ledger.attempt_no))`.

        **Never `attempt_count + 1`.** `quota_exhausted` is budget-exempt, so
        `attempt_count` does not move across a quota retry, and both tables
        carry `UNIQUE (run_id, attempt_no)`. A lease expiry keeps both rows, so
        the counter has to clear the higher of the two.
        """
        attempts = (
            await session.execute(
                select(func.coalesce(func.max(RunAttempt.attempt_no), 0)).where(
                    RunAttempt.run_id == run_id
                )
            )
        ).scalar_one()
        ledger = (
            await session.execute(
                select(func.coalesce(func.max(QuotaLedgerEntry.attempt_no), 0)).where(
                    QuotaLedgerEntry.run_id == run_id
                )
            )
        ).scalar_one()
        return int(max(attempts, ledger)) + 1

    # -- the true-up path ---------------------------------------------------

    async def true_up(
        self, session: AsyncSession, *, run_id: UUID, attempt_no: int, observed_seconds: int | None
    ) -> bool:
        """SPEC §7's release, guarded. Returns whether this call was the writer."""
        result = await session.execute(
            update(QuotaLedgerEntry)
            .where(
                QuotaLedgerEntry.run_id == run_id,
                QuotaLedgerEntry.attempt_no == attempt_no,
                QuotaLedgerEntry.actual_wallclock_s.is_(None),
            )
            .values(actual_wallclock_s=max(0, int(observed_seconds or 0)))
        )
        wrote = bool(result.rowcount)
        if wrote:
            logger.info(
                "quota.true_up",
                run_id=str(run_id),
                attempt_no=attempt_no,
                observed_seconds=int(observed_seconds or 0),
            )
        return wrote

    async def release(self, session: AsyncSession, run: Run, observed_seconds: int | None) -> None:
        """`QuotaPort.release`: settle this run's newest open reservation.

        The signature is fixed by callers that predate T7 and carries no
        `attempt_no`, so it is resolved here — the newest open row for the run,
        which is by construction the attempt that is ending — and the same
        `actual_wallclock_s IS NULL` guard applies. A run with no ledger row at
        all (a T5-era run, or a finalize re-drive after a cancel already closed
        it) is a no-op, never an error: raising would unwind somebody else's
        transaction.

        Uncapped on purpose (SPEC §7 meters wall clock): a run that overran its
        reservation reports the overrun. Capping at the reservation would
        under-report the window in exactly the case that matters.
        """
        attempt_no = (
            await session.execute(
                select(QuotaLedgerEntry.attempt_no)
                .where(
                    QuotaLedgerEntry.run_id == run.id,
                    QuotaLedgerEntry.actual_wallclock_s.is_(None),
                )
                .order_by(QuotaLedgerEntry.attempt_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if attempt_no is None:
            return
        await self.true_up(
            session, run_id=run.id, attempt_no=int(attempt_no), observed_seconds=observed_seconds
        )

    # -- the wake path ------------------------------------------------------

    async def earliest_headroom(
        self,
        session: AsyncSession,
        account: ProviderAccount,
        *,
        reservation_seconds: int,
        now: datetime,
    ) -> datetime | None:
        usage = await read_window(session, account.id, now=now)
        closed = await read_closed_in_window(
            session, account.id, now=now, window_hours=account.rolling_window_hours
        )
        return earliest_headroom_at(
            closed,
            usage=usage,
            now=now,
            window_hours=account.rolling_window_hours,
            ceiling_seconds=account.ceiling_seconds,
            reservation_seconds=reservation_seconds,
        )

    async def next_wake_at(
        self, session: AsyncSession, run: Run, exhausted_until: datetime | None
    ) -> datetime:
        """`QuotaPort`'s one new method (plan decision D9): SPEC §3.2's
        `blocked_quota` wake — "at `exhausted_until` / window headroom".

        Called from exactly one place, `advance_failed`'s budget-exempt branch,
        which is reached only on a genuine provider-exhaustion signal. So this
        is also the durable write site for `provider_accounts.exhausted_until`
        (D11): the transition and the account write land in one transaction.

        The stored value is never shortened — a later report wins, an earlier
        one is ignored — and nothing ever clears it: admission just compares it
        to `now`, so it decays by the passage of time.

        The never-shorten guard is a *conditional UPDATE*, not a Python
        compare-then-write: two interleaved transactions can both read the
        same `stored` value (or both read it `None`) before either writes, and
        a Python-side `until > stored` check would let the second writer
        overwrite unconditionally — exactly the shortening this method exists
        to forbid. The `WHERE ... exhausted_until IS NULL OR exhausted_until <
        :until` clause makes Postgres the referee: only a genuinely later
        `until` can ever move the column, whichever transaction commits last.
        """
        now = datetime.now(UTC)
        account = await resolve_account(session, provider=self._provider, label=self._label)
        if account is None:
            return exhausted_until or (now + timedelta(minutes=EXHAUSTED_FALLBACK_MINUTES))

        if exhausted_until is not None:
            until, source = exhausted_until, "cli"
        else:
            # The CLI said "limit reached" and gave no reset time. Refusing to
            # block would let the very next tick re-burn the account; the
            # source string is what keeps this invented number visible on the
            # operator's quota strip.
            until = now + timedelta(minutes=EXHAUSTED_FALLBACK_MINUTES)
            source = "cli_no_reset"

        written = (
            await session.execute(
                update(ProviderAccount)
                .where(
                    ProviderAccount.id == account.id,
                    or_(
                        ProviderAccount.exhausted_until.is_(None),
                        ProviderAccount.exhausted_until < until,
                    ),
                )
                .values(exhausted_until=until, exhausted_source=source)
                .returning(ProviderAccount.exhausted_until)
            )
        ).scalar_one_or_none()
        if written is not None:
            durable = written
        else:
            # The guard didn't fire: a stored value already at or past `until`
            # wins. Re-read rather than trust the pre-lock `account` object,
            # which may itself be stale relative to whichever write did land.
            durable = (
                await session.execute(
                    select(ProviderAccount.exhausted_until).where(ProviderAccount.id == account.id)
                )
            ).scalar_one()

        headroom = await self.earliest_headroom(
            session, account, reservation_seconds=self._typical, now=now
        )
        candidates = [durable] + ([headroom] if headroom is not None else [])
        return max(candidates)

    # -- SPEC §5c's recording seam (no poller in T7) ------------------------

    async def record_reading(
        self,
        session: AsyncSession,
        *,
        account_id: UUID,
        utilization_percent: float,
        source: str,
        at: datetime,
    ) -> None:
        """Readings are account metadata, never ledger rows — and admission
        already honours them (tightening only), so the seam is real rather than
        notional even though SPEC §5c's poller is explicitly out of scope."""
        await session.execute(
            update(ProviderAccount)
            .where(ProviderAccount.id == account_id)
            .values(
                last_reading_utilization=utilization_percent,
                last_reading_source=source,
                last_reading_at=at,
            )
        )


def _limits_of(account: ProviderAccount) -> AccountLimits:
    return AccountLimits(
        ceiling_seconds=account.ceiling_seconds,
        rolling_window_hours=account.rolling_window_hours,
        window_cap_runs=account.window_cap_runs,
        provider_window_capacity_seconds=account.provider_window_capacity_seconds,
        exhausted_until=account.exhausted_until,
        last_reading_utilization=(
            float(account.last_reading_utilization)
            if account.last_reading_utilization is not None
            else None
        ),
        last_reading_at=account.last_reading_at,
        is_active=account.is_active,
    )
