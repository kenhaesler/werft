import os
from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

TABLES = [  # truncation order = reverse dependency order
    "artifacts",
    "quota_ledger",
    "provider_accounts",
    "run_attempts",
    "run_events",
    "runs",
    "backlog_items",
    "project_events",
    "projects",
]


@pytest.fixture(scope="session")
def migrated_db(pg_url: str) -> str:
    os.environ["WERFT_TEST_DATABASE_URL"] = pg_url
    command.upgrade(Config("alembic.ini"), "head")
    return pg_url


@pytest.fixture
async def db_session(migrated_db: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(migrated_db)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    async with engine.begin() as conn:
        for t in TABLES:
            await conn.execute(text(f"TRUNCATE {t} CASCADE"))
    await engine.dispose()
