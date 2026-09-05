from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from werft.app import create_app
from werft.config.settings import Settings


def configured_app(tmp_path: Path):
    token = tmp_path / "api-token"
    token.write_text("test-token")
    return create_app(Settings(api_token_file=str(token), max_concurrent_runs=3))


async def test_system_requires_auth_before_docker_access(tmp_path: Path):
    app = configured_app(tmp_path)
    with patch("werft.api.system.DockerClient") as docker:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/system")
        assert response.status_code == 401
        docker.assert_not_called()


async def test_system_reports_host_and_only_werft_containers(tmp_path: Path):
    app = configured_app(tmp_path)
    docker = AsyncMock()
    docker.host_info.return_value = {
        "Name": "test-vm",
        "NCPU": 8,
        "MemTotal": 16000,
        "OperatingSystem": "Linux",
        "Architecture": "x86_64",
        "ServerVersion": "29.6.2",
        "SensitiveExtra": "never expose",
    }
    docker.list_containers.return_value = [
        {
            "Id": "abc",
            "Labels": {"werft.run_id": "run-1"},
            "Names": ["/agent-1"],
            "Image": "runner",
            "State": "running",
            "Status": "Up 2 minutes",
        },
        {"Id": "secret", "Labels": {"other": "private"}},
        {"Id": "unlabelled", "Labels": None},
    ]
    with patch("werft.api.system.DockerClient") as factory:
        factory.return_value.__aenter__ = AsyncMock(return_value=docker)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/system", headers={"Authorization": "Bearer test-token"}
            )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "test-vm"
    assert data["cpus"] == 8
    assert data["max_concurrent_runs"] == 3
    assert [c["id"] for c in data["containers"]] == ["abc"]
    assert data["containers"][0]["name"] == "agent-1"
    assert "SensitiveExtra" not in data


async def test_system_unavailable_does_not_leak_docker_error(tmp_path: Path):
    app = configured_app(tmp_path)
    docker = AsyncMock()
    docker.negotiate.side_effect = httpx.ConnectError("private socket details")
    with patch("werft.api.system.DockerClient") as factory:
        factory.return_value.__aenter__ = AsyncMock(return_value=docker)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/system", headers={"Authorization": "Bearer test-token"}
            )
    assert response.status_code == 503
    assert "private socket" not in response.text
