from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine_from_url(url: str) -> AsyncEngine:
    """One engine, one pool — the manager is the only writer (SPEC §3.3.1)."""
    return create_async_engine(url, pool_pre_ping=True)
