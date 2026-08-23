"""Lifecycle ordering, manager-side ceiling enforcement, and teardown (SPEC Â§4.3)."""

import asyncio

import pytest

from werft.runner.create_body import ProjectRunnerConfig, RunPlacement
from werft.runner.docker_api import DieEvent, DockerApiError
from werft.runner.lifecycle import (
    ADAPTER_CRASH,
    EXIT_CODE_MEANINGS,
    Completion,
    RunnerLifecycle,
    meaning_of,
)

DIGEST = "werft-runner-elastic@sha256:" + "b" * 64


class FakeClient:
    def __init__(self, *, die_codes=(0,), stall=False, inspect_code=137):
        self.calls: list[str] = []
        self._die_codes = list(die_codes)
        self._stall = stall
        self._inspect_code = inspect_code

    async def create_network(self, name):
        self.calls.append(f"create_network:{name}")
        return "net1"

    async def remove_network(self, name):
        self.calls.append(f"remove_network:{name}")

    async def create_container(self, name, body):
        self.calls.append(f"create_container:{name}")
        return "c1"

    async def start_container(self, container_id):
        self.calls.append(f"start:{container_id}")

    async def kill_container(self, container_id, signal="SIGKILL"):
        self.calls.append(f"kill:{container_id}:{signal}")

    async def remove_container(self, container_id, *, force=True):
        self.calls.append(f"remove_container:{container_id}")

    async def inspect_container(self, container_id):
        self.calls.append(f"inspect:{container_id}")
        return {"State": {"ExitCode": self._inspect_code}}

    async def watch_die_events(self, run_id, *, since=None):
        if self._stall:
            await asyncio.sleep(3600)
        for code in self._die_codes:
            yield DieEvent(container_id="c1", exit_code=code, run_id=run_id)


@pytest.fixture
def placement(tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "outputs").mkdir()
    (run_dir / "secrets").mkdir()
    (run_dir / "task.json").write_text("{}")
    return RunPlacement(
        run_id="run-1",
        container_name="werft-run-1",
        network_name="werft-run-1-net",
        dns_ip="10.90.7.53",
        run_dir=str(run_dir),
        workspace_dir=str(run_dir / "workspace"),
        outputs_dir=str(run_dir / "outputs"),
        task_json_path=str(run_dir / "task.json"),
        secrets_dir=str(run_dir / "secrets"),
        proxy_url="",
    )


@pytest.fixture
def config():
    return ProjectRunnerConfig(image_digest=DIGEST, memory_bytes=8 << 30, nano_cpus=4_000_000_000)


def test_exit_code_contract_matches_spec():
    assert EXIT_CODE_MEANINGS == {
        0: "contract_fulfilled",
        2: "cli_unstartable",
        4: "workspace_git_failure",
        5: "result_serialization_failure",
    }
    for unknown in (1, 3, 6, 137, 143):
        assert meaning_of(unknown) == ADAPTER_CRASH


async def test_launch_order_is_network_then_create_then_start(placement, config):
    client = FakeClient()
    lifecycle = RunnerLifecycle(client)
    await lifecycle.prepare(placement)
    await lifecycle.launch(placement, config, entrypoint=["/adapter"])
    assert client.calls == [
        "create_network:werft-run-1-net",
        "create_container:werft-run-1",
        "start:c1",
    ]


async def test_completion_reads_the_die_event_exit_code(placement, config):
    client = FakeClient(die_codes=(4,))
    lifecycle = RunnerLifecycle(client)
    completion = await lifecycle.await_completion(placement, "c1")
    assert completion == Completion(exit_code=4, timed_out=False)
    assert completion.meaning == "workspace_git_failure"
    assert not any(call.startswith("inspect:") for call in client.calls), (
        "the exit code rides on the die event; no inspect needed on the happy path"
    )


async def test_ceiling_is_enforced_manager_side_by_killing(placement, config):
    """SPEC Â§4.3: a root agent can patch the adapter, so the ceiling lives here."""
    client = FakeClient(stall=True, inspect_code=137)
    lifecycle = RunnerLifecycle(client, ceiling_seconds=0.05)
    completion = await lifecycle.await_completion(placement, "c1")
    assert completion.timed_out is True
    assert completion.exit_code == 137
    assert "kill:c1:SIGKILL" in client.calls


async def test_teardown_removes_container_and_network(placement):
    client = FakeClient()
    await RunnerLifecycle(client).teardown(placement, "c1")
    assert client.calls == ["remove_container:c1", "remove_network:werft-run-1-net"]


async def test_teardown_still_removes_the_network_when_launch_failed(placement):
    """No leaked networks even when no container was ever created."""
    client = FakeClient()
    await RunnerLifecycle(client).teardown(placement, None)
    assert client.calls == ["remove_network:werft-run-1-net"]


async def test_completion_falls_back_to_inspect_if_the_stream_ends(placement):
    client = FakeClient(die_codes=(), inspect_code=2)
    completion = await RunnerLifecycle(client).await_completion(placement, "c1")
    assert completion.exit_code == 2
    assert completion.meaning == "cli_unstartable"


async def test_launch_removes_the_container_when_start_fails(placement, config):
    """Otherwise the id is never returned and the container is orphaned — the
    caller cannot hand it to teardown because it never learned it."""

    class FailingStart(FakeClient):
        async def start_container(self, container_id):
            self.calls.append(f"start:{container_id}")
            raise DockerApiError(500, "no such image")

    client = FailingStart()
    with pytest.raises(DockerApiError):
        await RunnerLifecycle(client).launch(placement, config, entrypoint=["/adapter"])
    assert "remove_container:c1" in client.calls, (
        "a created-but-unstarted container must be removed"
    )


async def test_teardown_removes_the_network_even_if_container_removal_fails(placement):
    """The "no leaked networks" promise must hold off the happy path too."""

    class FailingRemove(FakeClient):
        async def remove_container(self, container_id, *, force=True):
            self.calls.append(f"remove_container:{container_id}")
            raise DockerApiError(500, "device or resource busy")

    client = FailingRemove()
    with pytest.raises(DockerApiError):
        await RunnerLifecycle(client).teardown(placement, "c1")
    assert "remove_network:werft-run-1-net" in client.calls


async def test_a_die_event_for_another_container_is_ignored(placement):
    """A stale or concurrent container carrying the same label must never supply
    this run's exit code."""

    class OtherContainerFirst(FakeClient):
        async def watch_die_events(self, run_id, *, since=None):
            yield DieEvent(container_id="someone-else", exit_code=0, run_id=run_id)
            yield DieEvent(container_id="c1", exit_code=4, run_id=run_id)

    completion = await RunnerLifecycle(OtherContainerFirst()).await_completion(placement, "c1")
    assert completion.exit_code == 4, "the other container's exit code must not be adopted"


async def test_an_event_with_no_container_id_matches_nothing(placement):
    class MalformedEvent(FakeClient):
        async def watch_die_events(self, run_id, *, since=None):
            yield DieEvent(container_id="", exit_code=0, run_id=run_id)

    completion = await RunnerLifecycle(MalformedEvent(inspect_code=7)).await_completion(
        placement, "c1"
    )
    assert completion.exit_code == 7, "a malformed event must fall through to inspect"


async def test_await_completion_passes_the_since_window_through(placement):
    """Without `since`, a container that exits before the stream is established
    is never seen and the run burns the entire ceiling."""
    seen = {}

    class RecordsSince(FakeClient):
        async def watch_die_events(self, run_id, *, since=None):
            seen["since"] = since
            yield DieEvent(container_id="c1", exit_code=0, run_id=run_id)

    await RunnerLifecycle(RecordsSince()).await_completion(placement, "c1", since=1785600000)
    assert seen["since"] == 1785600000


async def test_ceiling_path_survives_a_container_that_is_already_gone(placement):
    """inspect can 404 after the kill; that must not raise out of the timeout
    handler and lose the completion entirely."""

    class GoneOnInspect(FakeClient):
        async def inspect_container(self, container_id):
            raise DockerApiError(404, "no such container")

    lifecycle = RunnerLifecycle(GoneOnInspect(stall=True), ceiling_seconds=0.05)
    completion = await lifecycle.await_completion(placement, "c1")
    assert completion.timed_out is True
    assert completion.exit_code == -1
    assert completion.meaning == "adapter_crash", "an unknown exit is never 'contract fulfilled'"
