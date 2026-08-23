"""Protocol-level tests for the Engine API client, against a mock transport."""

import json

import httpx
import pytest

from werft.runner.docker_api import (
    API_VERSION,
    MAX_API_VERSION,
    MIN_API_VERSION,
    DockerApiError,
    DockerClient,
    _build_transport,
    is_pool_overlap,
    subnets_of,
)


def client_with(handler) -> DockerClient:
    client = DockerClient(url="http://docker-test")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://docker-test"
    )
    return client


def test_api_version_ceiling_matches_engine_29_6_2():
    assert MAX_API_VERSION == "1.52"
    assert API_VERSION == "v1.52"


async def test_negotiate_steps_down_to_an_older_daemon():
    """A hard pin turns a capable older daemon into an opaque 400."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/version", "GET /version must use the unversioned path"
        return httpx.Response(200, json={"ApiVersion": "1.48"})

    async with client_with(handler) as client:
        assert await client.negotiate() == "1.48"
        assert client.api_version == "1.48"


async def test_negotiate_never_exceeds_our_verified_ceiling():
    async with client_with(lambda r: httpx.Response(200, json={"ApiVersion": "1.99"})) as client:
        assert await client.negotiate() == MAX_API_VERSION


async def test_negotiate_refuses_a_daemon_below_the_floor():
    async with client_with(lambda r: httpx.Response(200, json={"ApiVersion": "1.24"})) as client:
        with pytest.raises(DockerApiError, match=f"below Werft's floor v{MIN_API_VERSION}"):
            await client.negotiate()


async def test_negotiated_version_is_used_in_request_paths():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json={"ApiVersion": "1.48"})
        seen["path"] = request.url.path
        return httpx.Response(201, json={"Id": "c1"})

    async with client_with(handler) as client:
        await client.negotiate()
        await client.create_container("n", {})
    assert seen["path"] == "/v1.48/containers/create"


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


async def test_create_network_without_subnet_is_byte_identical_to_today():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"Id": "net123"})

    async with client_with(handler) as client:
        await client.create_network("werft-run-abc-net")
    assert seen["body"] == {
        "Name": "werft-run-abc-net",
        "Driver": "bridge",
        "Internal": True,
        "CheckDuplicate": True,
    }
    assert "IPAM" not in seen["body"]


async def test_create_network_with_subnet_adds_ipam_config():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"Id": "net123"})

    async with client_with(handler) as client:
        await client.create_network("werft-run-abc-net", subnet="172.24.0.0/29")
    assert seen["body"]["IPAM"] == {
        "Driver": "default",
        "Config": [{"Subnet": "172.24.0.0/29"}],
    }
    assert seen["body"]["Internal"] is True
    assert seen["body"]["Name"] == "werft-run-abc-net"


async def test_connect_network_without_ipv4_omits_endpoint_config():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200)

    async with client_with(handler) as client:
        await client.connect_network("net123", "container1")
    assert seen["url"].endswith(f"/{API_VERSION}/networks/net123/connect")
    assert seen["body"] == {"Container": "container1"}


async def test_connect_network_with_ipv4_sets_endpoint_config():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200)

    async with client_with(handler) as client:
        await client.connect_network("net123", "container1", ipv4="172.24.0.2")
    assert seen["body"] == {
        "Container": "container1",
        "EndpointConfig": {"IPAMConfig": {"IPv4Address": "172.24.0.2"}},
    }


async def test_connect_network_raises_on_error():
    async with client_with(lambda r: httpx.Response(500, json={"message": "boom"})) as client:
        with pytest.raises(DockerApiError):
            await client.connect_network("net123", "container1")


async def test_disconnect_network_tolerates_404():
    async with client_with(lambda r: httpx.Response(404, json={"message": "no such"})) as client:
        await client.disconnect_network("net123", "container1")  # must not raise


async def test_disconnect_network_tolerates_not_connected_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, json={"message": "container container1 is not connected to network net123"}
        )

    async with client_with(handler) as client:
        await client.disconnect_network("net123", "container1")  # must not raise


async def test_disconnect_network_raises_on_unrelated_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    async with client_with(handler) as client:
        with pytest.raises(DockerApiError):
            await client.disconnect_network("net123", "container1")


async def test_disconnect_network_sends_force_by_default():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200)

    async with client_with(handler) as client:
        await client.disconnect_network("net123", "container1")
    assert seen["url"].endswith(f"/{API_VERSION}/networks/net123/disconnect")
    assert seen["body"] == {"Container": "container1", "Force": True}


async def test_disconnect_network_force_false_is_sent_verbatim():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200)

    async with client_with(handler) as client:
        await client.disconnect_network("net123", "container1", force=False)
    assert seen["body"] == {"Container": "container1", "Force": False}


def test_is_pool_overlap_true_on_403():
    exc = DockerApiError(403, "Pool overlaps with other one on this address space")
    assert is_pool_overlap(exc) is True


def test_is_pool_overlap_true_on_message_case_insensitive():
    exc = DockerApiError(500, "pool OVERLAPS with other one on this address space")
    assert is_pool_overlap(exc) is True


def test_is_pool_overlap_false_on_unrelated_error():
    exc = DockerApiError(500, "something else went wrong")
    assert is_pool_overlap(exc) is False


def test_tcp_url_builds_a_plain_http_client_no_unix_transport():
    _transport, base = _build_transport("tcp://docker-socket-proxy:2375")
    assert base == "http://docker-socket-proxy:2375"
    client = DockerClient(url="tcp://docker-socket-proxy:2375")
    assert str(client._client.base_url).rstrip("/") == "http://docker-socket-proxy:2375"


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


async def test_inspect_network_returns_body_on_200():
    body = {"Id": "net123", "Name": "werft-run-abc-net", "IPAM": {"Config": []}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/networks/werft-run-abc-net")
        return httpx.Response(200, json=body)

    async with client_with(handler) as client:
        assert await client.inspect_network("werft-run-abc-net") == body


async def test_inspect_network_returns_none_on_404():
    async with client_with(lambda r: httpx.Response(404, json={"message": "no such"})) as client:
        assert await client.inspect_network("gone") is None


async def test_inspect_network_raises_on_other_error():
    async with client_with(lambda r: httpx.Response(500, json={"message": "boom"})) as client:
        with pytest.raises(DockerApiError) as excinfo:
            await client.inspect_network("bad")
    assert excinfo.value.status_code == 500


def test_subnets_of_extracts_subnets_from_a_realistic_inspect_body():
    network = {
        "Id": "net123",
        "Name": "werft-run-abc-net",
        "IPAM": {
            "Driver": "default",
            "Config": [{"Subnet": "172.24.0.0/29", "Gateway": "172.24.0.1"}],
        },
    }
    assert subnets_of(network) == ["172.24.0.0/29"]


def test_subnets_of_none_is_empty():
    assert subnets_of(None) == []


def test_subnets_of_null_config_is_empty():
    assert subnets_of({"IPAM": {"Config": None}}) == []


def test_subnets_of_skips_entries_missing_subnet_key():
    network = {"IPAM": {"Config": [{"Gateway": "172.24.0.1"}, {"Subnet": "10.0.0.0/24"}]}}
    assert subnets_of(network) == ["10.0.0.0/24"]


def test_subnets_of_missing_ipam_is_empty():
    assert subnets_of({}) == []


def test_subnets_of_non_dict_config_entry_is_skipped_not_raised():
    """A malformed IPAM entry reaches `subnets_of` from a never-raises teardown
    path (driver `_capture_network_subnets`, sweeps' reap). `config["Subnet"]`
    on a non-dict would raise `TypeError` and skip the rest of the teardown —
    `remove_secrets` included."""
    assert subnets_of({"IPAM": {"Config": [42]}}) == []
    assert subnets_of({"IPAM": {"Config": [42, {"Subnet": "10.0.0.0/24"}]}}) == ["10.0.0.0/24"]


def test_subnets_of_non_list_config_is_empty():
    assert subnets_of({"IPAM": {"Config": {"Subnet": "10.0.0.0/24"}}}) == []


def test_subnets_of_non_dict_ipam_is_empty():
    assert subnets_of({"IPAM": []}) == []
