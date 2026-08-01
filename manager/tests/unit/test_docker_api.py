"""Protocol-level tests for the Engine API client, against a mock transport."""

import json

import httpx
import pytest

from werft.runner.docker_api import (
    API_VERSION,
    DockerApiError,
    DockerClient,
    _build_transport,
)


def client_with(handler) -> DockerClient:
    client = DockerClient(url="http://docker-test")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=f"http://docker-test/{API_VERSION}"
    )
    return client


def test_api_version_is_pinned_to_the_engine_29_6_2_version():
    assert API_VERSION == "v1.52"


def test_unix_url_builds_a_uds_transport():
    transport, base = _build_transport("unix:///var/run/docker.sock")
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    assert base == "http://docker"


async def test_create_network_is_internal():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"Id": "net123"})

    async with client_with(handler) as client:
        assert await client.create_network("werft-run-abc-net") == "net123"
    assert seen["url"].endswith(f"/{API_VERSION}/networks/create")
    assert seen["body"]["Internal"] is True, "a run network must have no route out"
    assert seen["body"]["Name"] == "werft-run-abc-net"


async def test_create_container_passes_name_as_a_query_parameter():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"Id": "c123"})

    async with client_with(handler) as client:
        assert await client.create_container("werft-run-abc", {"Image": "x@sha256:y"}) == "c123"
    assert "name=werft-run-abc" in seen["url"]
    assert f"/{API_VERSION}/containers/create" in seen["url"]
    assert seen["body"] == {"Image": "x@sha256:y"}


async def test_error_response_raises_with_daemon_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "conflict: name in use"})

    async with client_with(handler) as client:
        with pytest.raises(DockerApiError) as excinfo:
            await client.create_container("dup", {})
    assert excinfo.value.status_code == 409
    assert "name in use" in excinfo.value.message


@pytest.mark.parametrize("status", [404, 409])
async def test_kill_tolerates_already_gone(status):
    async with client_with(lambda r: httpx.Response(status, json={"message": "no"})) as client:
        await client.kill_container("c123")  # must not raise


async def test_remove_container_tolerates_404():
    async with client_with(lambda r: httpx.Response(404, json={"message": "no such"})) as client:
        await client.remove_container("gone")


async def test_remove_network_tolerates_404():
    async with client_with(lambda r: httpx.Response(404, json={"message": "no such"})) as client:
        await client.remove_network("gone")


async def test_watch_die_events_filters_by_run_label_and_parses_exit_code():
    seen = {}
    payload = json.dumps(
        {
            "Type": "container",
            "Action": "die",
            "Actor": {
                "ID": "c999",
                "Attributes": {"exitCode": "4", "werft.run_id": "run-1"},
            },
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, text=payload + "\n")

    async with client_with(handler) as client:
        events = [event async for event in client.watch_die_events("run-1")]

    filters = json.loads(seen["params"]["filters"])
    assert filters == {
        "label": ["werft.run_id=run-1"],
        "event": ["die"],
        "type": ["container"],
    }
    assert len(events) == 1
    assert events[0].container_id == "c999"
    assert events[0].exit_code == 4, "exitCode arrives as a string and must be coerced to int"
    assert events[0].run_id == "run-1"


async def test_watch_die_events_skips_blank_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        body = '\n{"Actor":{"ID":"c1","Attributes":{"exitCode":"0"}}}\n\n'
        return httpx.Response(200, text=body)

    async with client_with(handler) as client:
        events = [event async for event in client.watch_die_events("run-1")]
    assert [e.exit_code for e in events] == [0]
