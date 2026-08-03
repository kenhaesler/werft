import asyncio
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from werft.app import create_app
from werft.config.settings import Settings


async def test_healthz_reports_ok() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_create_app_without_creds_boots_clean_and_healthz_still_answers() -> None:
    """The composition root's own contract: no GitHub App creds configured
    (the default `Settings()`, as in tests/dev) means the lifespan never
    even tries to build an engine/httpx client/orchestrator — just
    `/healthz`, entered through the real lifespan context (not a bare
    `ASGITransport`, which never runs lifespan at all)."""
    app = create_app(Settings())
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def _generate_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


async def test_create_app_with_creds_starts_and_stops_orchestrator_within_five_seconds(
    tmp_path: Path, pg_url: str
) -> None:
    """With GitHub App creds configured, the lifespan builds the engine,
    httpx client, `AppAuth`, and `Orchestrator`, and starts `Orchestrator.run`
    as a background task on entry. `database_url` points at a real (if
    unmigrated) Postgres — `pg_url` — so the tick/poll loops' discovery
    queries hit a real connection rather than hanging on an unroutable
    host; any query failure (e.g. `projects` not existing yet) is caught
    and logged by the loop's own defensive wrapper, never raised here. The
    load-bearing assertion is latency: exiting the lifespan sets the stop
    event and awaits the orchestrator's background task, and that must
    complete well under 5 s.
    """
    key_file = tmp_path / "app-key.pem"
    key_file.write_text(_generate_private_key_pem())

    settings = Settings(
        database_url=pg_url,
        github_app_client_id="Iv1.test1234",
        github_app_private_key_file=str(key_file),
        github_api_url="https://github-api.invalid.test",
        tick_seconds=1,
        issue_poll_seconds=1,
        check_poll_seconds=1,
    )
    app = create_app(settings)
    lifespan_cm = app.router.lifespan_context(app)

    await lifespan_cm.__aenter__()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200
    finally:
        start = time.monotonic()
        await asyncio.wait_for(lifespan_cm.__aexit__(None, None, None), timeout=5)
        elapsed = time.monotonic() - start

    assert elapsed < 5
