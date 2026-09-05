from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from werft.api.capabilities import capabilities, project_events
from werft.config.settings import Settings


def _request(settings: Settings) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))


@pytest.mark.asyncio
async def test_capabilities_redacts_credentials_and_exposes_safe_dispatch_metadata(
    tmp_path,
) -> None:
    config = tmp_path / "dispatch.json"
    config.write_text(
        '{"projects":{"demo":{"image_digest":"registry/agent@sha256:abc",'
        '"model":"claude-sonnet","timeout_seconds":120,"memory_bytes":2147483648,'
        '"nano_cpus":1000000000}}}',
        encoding="utf-8",
    )
    secret = "super-secret-token"
    settings = Settings(
        dispatch_config_file=str(config),
        github_app_client_id="configured-client",
        github_app_private_key_file=secret,
        claude_credential_file="/private/credential",
        quota_ceiling_seconds=3600,
    )
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)

    response = await capabilities(_request(settings), session)
    payload = response.model_dump_json()

    assert response.readiness["dispatch"] == "validated"
    assert response.readiness["github"] == "configured"
    assert response.dispatch[0].model == "claude-sonnet"
    assert response.dispatch[0].image_digest.endswith("@sha256:abc")
    assert secret not in payload
    assert "/private/credential" not in payload


@pytest.mark.asyncio
async def test_capabilities_distinguishes_unavailable_github_from_configured(tmp_path) -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)

    response = await capabilities(_request(Settings()), session)
    configured = await capabilities(
        _request(Settings(github_app_client_id="client", github_app_private_key_file="/key.pem")),
        session,
    )

    assert response.capabilities["github"] is False
    assert response.readiness["github"] == "unconfigured"
    assert response.readiness["dispatch"] == "unconfigured"
    assert configured.capabilities["github"] is True
    assert configured.readiness["github"] == "configured"


@pytest.mark.asyncio
async def test_project_events_are_paginated_newest_first_and_guard_unknown_project() -> None:
    project_id = uuid4()
    rows = [
        SimpleNamespace(
            id=2,
            event_type="protection_applied",
            payload={"branch": "main"},
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    ]
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[1, 3])
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=result)

    response = await project_events(project_id, limit=1, offset=1, session=session)

    assert response.total == 3
    assert response.events[0].event_type == "protection_applied"
    statement = session.execute.await_args.args[0]
    assert "project_events" in str(statement)
    session.scalar = AsyncMock(return_value=0)
    with pytest.raises(Exception) as error:
        await project_events(project_id, session=session)
    assert getattr(error.value, "status_code", None) == 404
