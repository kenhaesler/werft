import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from werft.domain.db_url import apply_password_file

config = context.config


def _url() -> str:
    url = os.environ.get("WERFT_TEST_DATABASE_URL") or os.environ.get("WERFT_DATABASE_URL")
    if not url:
        raise RuntimeError("set WERFT_DATABASE_URL (or WERFT_TEST_DATABASE_URL) to run migrations")
    # SPEC §10: secrets are file mounts, never env. This entrypoint reads
    # `WERFT_DATABASE_URL` straight off the environment rather than through
    # `Settings` (the `db` layer may not import `config` — see
    # `werft.domain.db_url`'s docstring), so it applies the same file-mount
    # splice `create_app` does, directly, so `manager-migrate` reaches the
    # database with the real password exactly like the `manager` service does.
    return apply_password_file(url, os.environ.get("WERFT_DATABASE_PASSWORD_FILE", ""))


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    # Hand-written migrations only: autogenerate is never trusted with the
    # partial indexes and triggers this schema uses (lineage A§4.6).
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_do_run_migrations)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async())
