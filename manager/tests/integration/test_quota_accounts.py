import asyncio

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from werft.db.models import ProviderAccount
from werft.quota.accounts import (
    account_lock_key,
    ensure_provider_account,
    lock_account_key,
    resolve_account,
)


async def ensure(session, **over):
    base = dict(
        provider="claude",
        label="primary",
        ceiling_seconds=18000,
        rolling_window_hours=5,
        window_cap_runs=None,
        provider_window_capacity_seconds=None,
    )
    return await ensure_provider_account(session, **(base | over))


async def test_ensure_is_an_upsert_keyed_on_provider_label(db_session):
    """SPEC §7's one knob: config + restart is the operator surface, so a
    second boot with a different ceiling must *update* the row, not add a
    second account (`DO UPDATE`, not `DO NOTHING`)."""
    first = await ensure(db_session)
    second = await ensure(db_session, ceiling_seconds=9000, window_cap_runs=12)

    assert first == second
    account = await db_session.get(ProviderAccount, first, populate_existing=True)
    assert account.ceiling_seconds == 9000  # lowering refuses NEW reservations (SPEC §7)
    assert account.window_cap_runs == 12
    assert account.is_active is True


async def test_ensure_reactivates_a_row_an_operator_had_disabled(db_session):
    account_id = await ensure(db_session)
    await db_session.execute(
        update(ProviderAccount).where(ProviderAccount.id == account_id).values(is_active=False)
    )
    await ensure(db_session)
    assert (await db_session.get(ProviderAccount, account_id, populate_existing=True)).is_active


async def test_resolve_is_none_when_there_is_no_row_at_all(db_session):
    assert await resolve_account(db_session, provider="claude", label="primary") is None


async def test_resolve_ignores_an_inactive_row(db_session):
    account_id = await ensure(db_session)
    await db_session.execute(
        update(ProviderAccount).where(ProviderAccount.id == account_id).values(is_active=False)
    )
    assert await resolve_account(db_session, provider="claude", label="primary") is None


async def test_the_advisory_lock_is_exclusive_and_transaction_scoped(db_session, migrated_db):
    """`pg_advisory_xact_lock` is what serialises admission (SPEC §3.3 item 3).
    Prove it blocks a second session and is released by COMMIT, not by the end
    of the statement — there is no unlock call anywhere in this codebase."""
    await db_session.commit()
    engine = create_async_engine(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key = account_lock_key("claude", "primary")
    try:
        async with factory() as a, factory() as b:
            async with a.begin():
                await lock_account_key(a, "claude", "primary")
                async with b.begin():
                    taken = await asyncio.wait_for(
                        b.execute(
                            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                            {"k": key},
                        ),
                        timeout=5,
                    )
                    assert taken.scalar_one() is False
            async with factory() as c, c.begin():
                taken = await c.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key}
                )
                assert taken.scalar_one() is True  # released at the first COMMIT
    finally:
        await engine.dispose()
