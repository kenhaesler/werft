"""Issue #19 acceptance: alembic up / down / up is clean.

These tests are deliberately sync: alembic's async env.py calls
asyncio.run(), which must not happen inside an already-running loop.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator

import asyncpg
import pytest
from alembic import command
from alembic.config import Config


def _plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _execute(url: str, sql: str) -> None:
    conn = await asyncpg.connect(_plain(url))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _fetchval(url: str, sql: str):
    conn = await asyncpg.connect(_plain(url))
    try:
        return await conn.fetchval(sql)
    finally:
        await conn.close()


@pytest.fixture
def scratch_db(pg_url: str) -> Iterator[str]:
    """A throwaway database so the cycle test never fights the session-scoped one."""
    name = f"werft_mig_{uuid.uuid4().hex[:8]}"
    asyncio.run(_execute(pg_url, f'CREATE DATABASE "{name}"'))
    base = pg_url.rsplit("/", 1)[0]
    yield f"{base}/{name}"
    asyncio.run(_execute(pg_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


def _alembic(url: str) -> Config:
    os.environ["WERFT_TEST_DATABASE_URL"] = url
    return Config("alembic.ini")


def test_upgrade_downgrade_upgrade_clean(scratch_db: str) -> None:
    cfg = _alembic(scratch_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    # after down/up the DB is fully re-seeded
    assert asyncio.run(_fetchval(scratch_db, "SELECT count(*) FROM run_status_transitions")) == 34
    assert asyncio.run(_fetchval(scratch_db, "SELECT count(*) FROM run_statuses")) == 11


def test_downgrade_leaves_nothing(scratch_db: str) -> None:
    cfg = _alembic(scratch_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    n = asyncio.run(
        _fetchval(
            scratch_db,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name <> 'alembic_version'",
        )
    )
    assert n == 0
