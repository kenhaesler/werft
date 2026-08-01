import os
from collections.abc import Iterator

import pytest
from testcontainers.community.postgres import PostgresContainer


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """Postgres 18 for integration tests.

    CI provides WERFT_TEST_DATABASE_URL (service container); locally,
    testcontainers spins up postgres:18 on demand.
    """
    env_url = os.environ.get("WERFT_TEST_DATABASE_URL")
    if env_url:
        yield env_url
        return
    with PostgresContainer("postgres:18", driver="asyncpg") as pg:
        yield pg.get_connection_url()
