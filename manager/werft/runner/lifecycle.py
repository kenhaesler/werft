"""Per-run container lifecycle (SPEC §4.3).

The order is fixed: network create -> container create -> start -> (die) ->
exit code -> read outputs -> remove container -> remove network. Completion is
the `die` event plus the inspected exit code plus `result.json` — never a
blocking wait, never log content.

The run ceiling, the kill and the teardown are enforced **here**, manager-side,
because a root agent can kill or patch the in-container adapter (SPEC §4.3).
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from werft.runner.create_body import ProjectRunnerConfig, RunPlacement, build_create_body
from werft.runner.docker_api import DockerClient

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


@dataclass(frozen=True)
class Completion:
    exit_code: int
    timed_out: bool

    @property
    def meaning(self) -> str:
        return meaning_of(self.exit_code)


class RunnerLifecycle:
    def __init__(
        self, client: DockerClient, *, ceiling_seconds: int = DEFAULT_CEILING_SECONDS
    ) -> None:
        self._client = client
        self._ceiling_seconds = ceiling_seconds

    async def prepare(self, placement: RunPlacement) -> None:
        await self._client.create_network(placement.network_name)

    async def launch(
        self,
        placement: RunPlacement,
        config: ProjectRunnerConfig,
        *,
        entrypoint: list[str],
    ) -> str:
        body: dict[str, Any] = build_create_body(placement, config, entrypoint=entrypoint)
        container_id = await self._client.create_container(placement.container_name, body)
        await self._client.start_container(container_id)
        return container_id

    async def await_completion(self, placement: RunPlacement, container_id: str) -> Completion:
        """Wait for the die event, bounded by the manager-enforced ceiling."""
        try:
            exit_code = await asyncio.wait_for(
                self._first_die(placement.run_id, container_id), timeout=self._ceiling_seconds
            )
            return Completion(exit_code=exit_code, timed_out=False)
        except TimeoutError:
            # The ceiling is ours to enforce; the in-container adapter cannot be trusted.
            await self._client.kill_container(container_id, signal="SIGKILL")
            inspected = await self._inspect_exit_code(container_id)
            return Completion(exit_code=inspected, timed_out=True)

    async def _first_die(self, run_id: str, container_id: str) -> int:
        async for event in self._client.watch_die_events(run_id):
            if event.container_id in ("", container_id) or container_id.startswith(
                event.container_id
            ):
                return event.exit_code
        # The stream ended without a die event: fall back to reconciliation inspect.
        return await self._inspect_exit_code(container_id)

    async def _inspect_exit_code(self, container_id: str) -> int:
        state = (await self._client.inspect_container(container_id)).get("State", {})
        return int(state.get("ExitCode", -1))

    async def teardown(self, placement: RunPlacement, container_id: str | None) -> None:
        """Always runs, including after a failed launch — no leaked containers or networks."""
        if container_id:
            await self._client.remove_container(container_id, force=True)
        await self._client.remove_network(placement.network_name)
