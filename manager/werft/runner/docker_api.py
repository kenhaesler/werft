"""A minimal async Docker Engine API client — only the calls the thin loop needs.

Deliberately small: `docker-py` is sync, pulls a large dependency surface, and its
convenience layer hides exactly the create-body fields SPEC §4.2 requires Werft to
control byte-for-byte. The manager reaches the daemon through docker-socket-proxy
(SPEC §1), which does not inspect request bodies — so manager code is the only
enforcement point and it must build the body itself.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

#: Docker CE 29.6.2 (the SPEC §2 floor) serves API v1.52 — bumped in 29.0.0 and
#: unchanged through 29.6.2. This is the highest version Werft's field usage has
#: been verified against, so it is a ceiling, never a demand: `negotiate()` steps
#: down to whatever the daemon actually serves. Hard-pinning would turn a
#: perfectly capable older daemon into an opaque `400 client version too new`.
MAX_API_VERSION = "1.52"

#: Below this the fields this module relies on are not all present.
MIN_API_VERSION = "1.41"

#: Back-compat alias for the pinned ceiling, in the path form used in URLs.
API_VERSION = f"v{MAX_API_VERSION}"

DEFAULT_SOCKET_URL = "unix:///var/run/docker.sock"


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.strip().lstrip("v").split("."))


def subnets_of(network: dict[str, Any] | None) -> list[str]:
    """Extract the IPAM subnets from a `network inspect` body.

    Tolerates every degenerate shape (None network, missing IPAM/Config, a
    null Config, a non-dict Config entry, or a Config entry without a Subnet
    key) by returning []. The callers sit on never-raises teardown paths, so
    "malformed body" must degrade to "no subnets", never to a `TypeError` that
    skips the rest of the teardown.
    """
    if not isinstance(network, dict):
        return []
    ipam = network.get("IPAM")
    configs = (ipam if isinstance(ipam, dict) else {}).get("Config")
    if not isinstance(configs, list):
        return []
    return [
        config["Subnet"] for config in configs if isinstance(config, dict) and "Subnet" in config
    ]


class DockerApiError(Exception):
    """A daemon call failed. Carries the status and the daemon's own message."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"docker api {status_code}: {message}")
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class DieEvent:
    """A container `die` event. The exit code rides on the event itself
    (Actor.Attributes.exitCode); reconciliation inspect remains the fallback."""

    container_id: str
    exit_code: int
    run_id: str | None


def _build_transport(url: str) -> tuple[httpx.AsyncBaseTransport, str]:
    if url.startswith("unix://"):
        return httpx.AsyncHTTPTransport(uds=url.removeprefix("unix://")), "http://docker"
    if url.startswith("tcp://"):
        return httpx.AsyncHTTPTransport(), "http://" + url.removeprefix("tcp://").rstrip("/")
    return httpx.AsyncHTTPTransport(), url.rstrip("/")


def is_pool_overlap(exc: DockerApiError) -> bool:
    """True when the daemon refused a network create for address-pool overlap.

    Docker's own refusal is the slot lock (SPEC intent): callers claim a run
    network by attempting create-with-subnet and treating this as "taken".
    The daemon has been observed to answer both with `403` and with a `500`
    whose message names the collision — so either signal counts.
    """
    return exc.status_code == 403 or "pool overlaps" in exc.message.lower()


class DockerClient:
    """One client per manager process. Close it on shutdown."""

    def __init__(self, url: str = DEFAULT_SOCKET_URL, *, timeout: float = 30.0) -> None:
        transport, base = _build_transport(url)
        self._client = httpx.AsyncClient(transport=transport, base_url=base, timeout=timeout)
        self._version = MAX_API_VERSION

    @property
    def api_version(self) -> str:
        return self._version

    def _path(self, path: str) -> str:
        return f"/v{self._version}{path}"

    async def negotiate(self) -> str:
        """Step down to the daemon's API version if it is older than our ceiling.

        `GET /version` is served on the unversioned path by every daemon, so this
        works even when our ceiling is too new for it.
        """
        response = await self._client.get("/version")
        self._check(response)
        served = str(response.json().get("ApiVersion", MAX_API_VERSION))
        if _as_tuple(served) < _as_tuple(MIN_API_VERSION):
            raise DockerApiError(
                0,
                f"daemon serves API v{served}, below Werft's floor v{MIN_API_VERSION}",
            )
        self._version = (
            served if _as_tuple(served) < _as_tuple(MAX_API_VERSION) else MAX_API_VERSION
        )
        return self._version

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> DockerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        # On a streamed response the body has not been read yet, and touching
        # .text/.json() would raise httpx.ResponseNotRead — masking the real
        # daemon error behind an httpx internal.
        if not response.is_closed and not hasattr(response, "_content"):
            try:
                response.read()
            except Exception:
                raise DockerApiError(response.status_code, "<unreadable body>") from None
        try:
            message = response.json().get("message", response.text)
        except ValueError:
            message = response.text
        raise DockerApiError(response.status_code, message)

    # --- networks ---------------------------------------------------------

    async def create_network(self, name: str, *, subnet: str | None = None) -> str:
        """Create the run's own internal network — no route to anywhere (SPEC §4.2).

        `Internal: True` never varies. When `subnet` is given, it is passed as
        an explicit IPAM pool — Docker's pool-overlap refusal (`is_pool_overlap`)
        is then the slot lock a later task's driver relies on to claim a
        per-slot run network.
        """
        body: dict[str, Any] = {
            "Name": name,
            "Driver": "bridge",
            "Internal": True,
            "CheckDuplicate": True,
        }
        if subnet is not None:
            body["IPAM"] = {"Driver": "default", "Config": [{"Subnet": subnet}]}
        response = await self._client.post(self._path("/networks/create"), json=body)
        self._check(response)
        return response.json()["Id"]

    async def remove_network(self, name_or_id: str) -> None:
        response = await self._client.delete(self._path(f"/networks/{quote(name_or_id, safe='')}"))
        if response.status_code == 404:
            return
        self._check(response)

    async def connect_network(
        self, network: str, container: str, *, ipv4: str | None = None
    ) -> None:
        body: dict[str, Any] = {"Container": container}
        if ipv4 is not None:
            body["EndpointConfig"] = {"IPAMConfig": {"IPv4Address": ipv4}}
        response = await self._client.post(
            self._path(f"/networks/{quote(network, safe='')}/connect"), json=body
        )
        self._check(response)

    async def disconnect_network(self, network: str, container: str, *, force: bool = True) -> None:
        response = await self._client.post(
            self._path(f"/networks/{quote(network, safe='')}/disconnect"),
            json={"Container": container, "Force": force},
        )
        if response.status_code == 404:
            return
        if response.status_code == 500:
            try:
                message = response.json().get("message", "")
            except ValueError:
                message = response.text
            if "is not connected" in message.lower():
                return
        self._check(response)

    async def list_networks(self) -> list[dict[str, Any]]:
        response = await self._client.get(self._path("/networks"))
        self._check(response)
        return response.json()

    async def inspect_network(self, name_or_id: str) -> dict[str, Any] | None:
        response = await self._client.get(self._path(f"/networks/{quote(name_or_id, safe='')}"))
        if response.status_code == 404:
            return None
        self._check(response)
        return response.json()

    # --- containers -------------------------------------------------------

    async def create_container(self, name: str, body: dict[str, Any]) -> str:
        response = await self._client.post(
            self._path("/containers/create"), params={"name": name}, json=body
        )
        self._check(response)
        return response.json()["Id"]

    async def start_container(self, container_id: str) -> None:
        response = await self._client.post(
            self._path(f"/containers/{quote(container_id, safe='')}/start")
        )
        self._check(response)

    async def inspect_container(self, container_id: str) -> dict[str, Any]:
        response = await self._client.get(
            self._path(f"/containers/{quote(container_id, safe='')}/json")
        )
        self._check(response)
        return response.json()

    async def kill_container(self, container_id: str, signal: str = "SIGKILL") -> None:
        """Manager-side enforcement (SPEC §4.3): a root agent can patch the
        in-container adapter, so the ceiling and tree-kill live out here."""
        response = await self._client.post(
            self._path(f"/containers/{quote(container_id, safe='')}/kill"),
            params={"signal": signal},
        )
        if response.status_code in (404, 409):  # already gone / not running
            return
        self._check(response)

    async def remove_container(self, container_id: str, *, force: bool = True) -> None:
        response = await self._client.delete(
            self._path(f"/containers/{quote(container_id, safe='')}"),
            params={"force": "true" if force else "false", "v": "true"},
        )
        if response.status_code == 404:
            return
        self._check(response)

    async def list_containers(self, *, all_: bool = True) -> list[dict[str, Any]]:
        response = await self._client.get(
            self._path("/containers/json"), params={"all": "true" if all_ else "false"}
        )
        self._check(response)
        return response.json()

    async def container_disk_usage_bytes(self, container_id: str) -> int:
        """Bytes written to the container's own writable layer.

        SPEC §4.2: "disk bounded by manager-side polling with hard kill". The
        poller lives in the orchestrator; this is the reading it polls. Bind
        mounts are not counted here — those are on Werft-owned filesystems and
        are bounded by the XFS project quota (SPEC §10).
        """
        response = await self._client.get(
            self._path(f"/containers/{quote(container_id, safe='')}/json"), params={"size": "true"}
        )
        self._check(response)
        return int(response.json().get("SizeRw") or 0)

    # --- events -----------------------------------------------------------

    async def watch_die_events(
        self, run_id: str, *, since: int | None = None
    ) -> AsyncIterator[DieEvent]:
        """Stream `die` events for one run. Never a blocking wait, never log content.

        `since` is not optional in practice: a container that exits between
        `start` and the stream being established emits its die event into the
        gap, and without a replay window the manager waits out the entire run
        ceiling for an event that already happened. Callers pass a timestamp
        taken *before* the container was created.
        """
        filters = json.dumps(
            {"label": [f"werft.run_id={run_id}"], "event": ["die"], "type": ["container"]}
        )
        params: dict[str, Any] = {"filters": filters}
        if since is not None:
            params["since"] = str(since)
        async with self._client.stream(
            "GET", self._path("/events"), params=params, timeout=None
        ) as response:
            self._check(response)
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                attributes = event.get("Actor", {}).get("Attributes", {})
                # -1 when the daemon reports no exitCode: an unknown exit must
                # never be mistaken for 0, which lifecycle reads as
                # "contract fulfilled".
                yield DieEvent(
                    container_id=event.get("Actor", {}).get("ID", event.get("id", "")),
                    exit_code=int(attributes.get("exitCode", -1)),
                    run_id=attributes.get("werft.run_id"),
                )
