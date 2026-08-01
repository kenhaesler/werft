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
    return httpx.AsyncHTTPTransport(), url.rstrip("/")


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
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise DockerApiError(response.status_code, message)

    # --- networks ---------------------------------------------------------

    async def create_network(self, name: str) -> str:
        """Create the run's own internal network — no route to anywhere (SPEC §4.2)."""
        response = await self._client.post(
            self._path("/networks/create"),
            json={"Name": name, "Driver": "bridge", "Internal": True, "CheckDuplicate": True},
        )
        self._check(response)
        return response.json()["Id"]

    async def remove_network(self, name_or_id: str) -> None:
        response = await self._client.delete(self._path(f"/networks/{quote(name_or_id, safe='')}"))
        if response.status_code == 404:
            return
        self._check(response)

    async def list_networks(self) -> list[dict[str, Any]]:
        response = await self._client.get(self._path("/networks"))
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

    # --- events -----------------------------------------------------------

    async def watch_die_events(self, run_id: str) -> AsyncIterator[DieEvent]:
        """Stream `die` events for one run. Never a blocking wait, never log content."""
        filters = json.dumps(
            {"label": [f"werft.run_id={run_id}"], "event": ["die"], "type": ["container"]}
        )
        async with self._client.stream(
            "GET", self._path("/events"), params={"filters": filters}, timeout=None
        ) as response:
            self._check(response)
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                attributes = event.get("Actor", {}).get("Attributes", {})
                yield DieEvent(
                    container_id=event.get("Actor", {}).get("ID", event.get("id", "")),
                    exit_code=int(attributes.get("exitCode", -1)),
                    run_id=attributes.get("werft.run_id"),
                )
