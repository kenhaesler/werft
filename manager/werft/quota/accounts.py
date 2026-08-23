"""Provider accounts: the row admission reads, and the lock admission takes.

`0001_spine.py` seeds no `provider_accounts` row on purpose — a ceiling is an
operator fact, and inventing one would be exactly the "make up a number"
failure SPEC §7 forbids. The row is *declared in settings* and reconciled at
startup (plan decision D4): config plus restart is the operator surface, the
same one every secret already uses. There is no seed script, no migration and
no API endpoint, because SPEC §9 closes the write set.

`DO UPDATE`, not `DO NOTHING`: an operator who lowers `WERFT_QUOTA_CEILING_
SECONDS` and restarts must actually get the lower ceiling. SPEC §7 makes that
safe — "lowering a ceiling refuses new reservations; it never kills in-flight
work" — because admission runs only *before* a reservation is taken.
"""

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from werft.db.models import ProviderAccount


async def ensure_provider_account(
    session: AsyncSession,
    *,
    provider: str,
    label: str,
    ceiling_seconds: int,
    rolling_window_hours: int,
    window_cap_runs: int | None,
    provider_window_capacity_seconds: int | None,
) -> UUID:
    values = {
        "provider": provider,
        "label": label,
        "ceiling_seconds": ceiling_seconds,
        "rolling_window_hours": rolling_window_hours,
        "window_cap_runs": window_cap_runs,
        "provider_window_capacity_seconds": provider_window_capacity_seconds,
        "is_active": True,
    }
    stmt = (
        insert(ProviderAccount)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[ProviderAccount.provider, ProviderAccount.label],
            set_={k: v for k, v in values.items() if k not in ("provider", "label")},
        )
        .returning(ProviderAccount.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def resolve_account(
    session: AsyncSession, *, provider: str, label: str
) -> ProviderAccount | None:
    """`None` means "no active account is configured".

    Deliberately not an exception: a missing account is a *system-wide*
    misconfiguration, not a verdict on any one run, and the dispatch sweep
    answers it by logging and returning rather than by parking the queue
    (plan decision D4).
    """
    return (
        await session.execute(
            select(ProviderAccount).where(
                ProviderAccount.provider == provider,
                ProviderAccount.label == label,
                ProviderAccount.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


def account_lock_key(provider: str, label: str) -> str:
    """The advisory-lock key. Derived from settings rather than from the
    account row's id, which is what lets the claim transaction take the lock
    *first* — before it reads anything at all (plan decision D2)."""
    return f"werft:quota:{provider}:{label}"


async def lock_account_key(session: AsyncSession, provider: str, label: str) -> None:
    """Transaction-scoped: COMMIT or ROLLBACK releases it, and no code path
    can forget to. Taken before any row lock, always, so every claim
    transaction queues in one order and two of them cannot deadlock."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": account_lock_key(provider, label)},
    )
