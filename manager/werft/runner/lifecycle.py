"""Per-run container lifecycle (SPEC §4.3).

The order is fixed: network create -> container create -> start -> (die) ->
exit code -> read outputs -> remove container -> remove network. Completion is
the `die` event plus the inspected exit code plus `result.json` — never a
blocking wait, never log content.

The run ceiling, the kill and the teardown are enforced **here**, manager-side,
because a root agent can kill or patch the in-container adapter (SPEC §4.3).
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from werft.runner.create_body import ProjectRunnerConfig, RunPlacement, build_create_body
from werft.runner.docker_api import DockerApiError, DockerClient

#: SPEC §4.3 exit-code contract. Anything else is an adapter crash.
EXIT_CODE_MEANINGS: dict[int, str] = {
    0: "contract_fulfilled",
    2: "cli_unstartable",
    4: "workspace_git_failure",
    5: "result_serialization_failure",
}
ADAPTER_CRASH = "adapter_crash"

DEFAULT_CEILING_SECONDS = 90 * 60  # SPEC §4.3: 90 min default


def meaning_of(exit_code: int) -> str:
    return EXIT_CODE_MEANINGS.get(exit_code, ADAPTER_CRASH)


def now_epoch_seconds() -> int:
    """Timestamp for the events `since` window, taken before container creation."""
    return int(time.time())


@dataclass(frozen=True)
class Completion:
    exit_code: int
    timed_out: bool

    @property
    def meaning(self) -> str:
        return meaning_of(self.exit_code)


class RunnerLifecycle:
    def __init__(
        self, client: DockerClient, *, ceiling_seconds: float = DEFAULT_CEILING_SECONDS
    ) -> None:
        self._client = client
        self._ceiling_seconds = ceiling_seconds

    async def prepare(self, placement: RunPlacement, *, subnet: str | None = None) -> None:
        """Create the run's network. `subnet` pins its IPAM pool to one egress
        slot's /24 — the daemon's pool-overlap refusal is then the slot lock
        (`docker_api.is_pool_overlap`), which is why the caller, not this
        method, decides which slot to try next. `None` (the default) is the
        egress-off shape: an unpinned Internal-only network, exactly as before."""
        await self._client.create_network(placement.network_name, subnet=subnet)

    async def launch(
        self,
        placement: RunPlacement,
        config: ProjectRunnerConfig,
        *,
        entrypoint: list[str],
    ) -> str:
        """Create and start the container.

        If `start` fails the container still exists, so it is removed here rather
        than orphaned: the id has not been returned yet, so the caller could not
        pass it to `teardown` even if it wanted to.
        """
        body: dict[str, Any] = build_create_body(placement, config, entrypoint=entrypoint)
        container_id = await self._client.create_container(placement.container_name, body)
        try:
            await self._client.start_container(container_id)
        except BaseException:
            await self._safe_remove_container(container_id)
            raise
        return container_id

    async def await_completion(
        self, placement: RunPlacement, container_id: str, *, since: int | None = None
    ) -> Completion:
        """Wait for the die event, bounded by the manager-enforced ceiling."""
        try:
            exit_code = await asyncio.wait_for(
                self._first_die(placement.run_id, container_id, since=since),
                timeout=self._ceiling_seconds,
            )
            return Completion(exit_code=exit_code, timed_out=False)
        except TimeoutError:
            # The ceiling is ours to enforce; the in-container adapter cannot be
            # trusted to enforce its own.
            await self._client.kill_container(container_id, signal="SIGKILL")
            return Completion(exit_code=await self._inspect_exit_code(container_id), timed_out=True)

    async def _first_die(self, run_id: str, container_id: str, *, since: int | None) -> int:
        """Return the exit code of *this* container's die event.

        Events for other containers carrying the same label are ignored rather
        than accepted: a stale or concurrent container must never supply this
        run's exit code.
        """
        async for event in self._client.watch_die_events(run_id, since=since):
            if self._is_this_container(event.container_id, container_id):
                return event.exit_code
        # The stream ended without a matching die event: fall back to the
        # reconciliation inspect SPEC §4.3 names alongside the events stream.
        return await self._inspect_exit_code(container_id)

    @staticmethod
    def _is_this_container(event_id: str, container_id: str) -> bool:
        """Ids may be full or truncated depending on the caller; an empty id
        matches nothing, so a malformed event cannot claim to be this run."""
        if not event_id or not container_id:
            return False
        return event_id.startswith(container_id) or container_id.startswith(event_id)

    async def _inspect_exit_code(self, container_id: str) -> int:
        try:
            state = (await self._client.inspect_container(container_id)).get("State", {})
        except DockerApiError:
            # Already removed out of band: the exit code is unknowable, and -1
            # reads as an adapter crash rather than as success.
            return -1
        return int(state.get("ExitCode", -1))

    async def teardown(
        self,
        placement: RunPlacement,
        container_id: str | None,
        *,
        before_network_remove: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Always runs, including after a failed launch.

        The network removal sits in a `finally` so a failing container removal
        cannot strand it — otherwise the "no leaked networks" promise holds only
        on the happy path.

        `before_network_remove` is the one seam between "the run's container is
        gone" and "the run's network is gone": the egress slot's service
        containers must be detached in exactly that window, or the removal
        fails with active endpoints. It is called at most once and its failure
        is swallowed — a hook that raised must not be the reason a network
        leaks.
        """
        try:
            if container_id:
                await self._client.remove_container(container_id, force=True)
        finally:
            if before_network_remove is not None:
                # Suppressed, not logged: the hook owns its own logging, and
                # nothing it could raise is worth stranding the network for.
                with contextlib.suppress(Exception):
                    await before_network_remove()
            await self._client.remove_network(placement.network_name)

    async def _safe_remove_container(self, container_id: str) -> None:
        # Suppressed deliberately: this runs while another exception is in
        # flight, and the caller's teardown still needs to remove the network.
        with contextlib.suppress(DockerApiError):
            await self._client.remove_container(container_id, force=True)
