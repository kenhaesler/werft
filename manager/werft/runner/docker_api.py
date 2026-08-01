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

#: Docker CE 29.6.2 serves API v1.52 (bumped in 29.0.0, unchanged through 29.6.2).
API_VERSION = "v1.52"

DEFAULT_SOCKET_URL = "unix:///var/run/docker.sock"


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
        self._client = httpx.AsyncClient(
            transport=transport, base_url=f"{base}/{API_VERSION}", timeout=timeout
        )

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
            "/networks/create",
            json={"Name": name, "Driver": "bridge", "Internal": True, "CheckDuplicate": True},
        )
        self._check(response)
        return response.json()["Id"]

    async def remove_network(self, name_or_id: str) -> None:
        response = await self._client.delete(f"/networks/{quote(name_or_id, safe='')}")
        if response.status_code == 404:
            return
        self._check(response)

    async def list_networks(self) -> list[dict[str, Any]]:
        response = await self._client.get("/networks")
        self._check(response)
        return response.json()

    # --- containers -------------------------------------------------------

    async def create_container(self, name: str, body: dict[str, Any]) -> str:
        response = await self._client.post("/containers/create", params={"name": name}, json=body)
        self._check(response)
        return response.json()["Id"]

    async def start_container(self, container_id: str) -> None:
        response = await self._client.post(f"/containers/{quote(container_id, safe='')}/start")
        self._check(response)

    async def inspect_container(self, container_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/containers/{quote(container_id, safe='')}/json")
        self._check(response)
        return response.json()

    async def kill_container(self, container_id: str, signal: str = "SIGKILL") -> None:
        """Manager-side enforcement (SPEC §4.3): a root agent can patch the
        in-container adapter, so the ceiling and tree-kill live out here."""
        response = await self._client.post(
            f"/containers/{quote(container_id, safe='')}/kill", params={"signal": signal}
        )
        if response.status_code in (404, 409):  # already gone / not running
            return
        self._check(response)

    async def remove_container(self, container_id: str, *, force: bool = True) -> None:
        response = await self._client.delete(
            f"/containers/{quote(container_id, safe='')}",
            params={"force": "true" if force else "false", "v": "true"},
        )
        if response.status_code == 404:
            return
        self._check(response)

    async def list_containers(self, *, all_: bool = True) -> list[dict[str, Any]]:
        response = await self._client.get(
            "/containers/json", params={"all": "true" if all_ else "false"}
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
            "GET", "/events", params={"filters": filters}, timeout=None
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
