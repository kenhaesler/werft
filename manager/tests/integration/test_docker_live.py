"""Executed acceptance for issue #20, against a real Docker daemon.

Skipped where no daemon socket exists (Windows dev box); runs in CI on
ubuntu-24.04. SELinux-label behaviour (:Z / :z) and `ps -eZ` showing
`container_t` are NOT falsifiable here — they need the Rocky Linux 10 host with
SELinux enforcing, and are asserted by install.sh at T9.
"""

import asyncio
import os
import uuid

import pytest

from werft.runner.collect import collect_outputs
from werft.runner.create_body import ProjectRunnerConfig, RunPlacement
from werft.runner.docker_api import DockerClient
from werft.runner.lifecycle import RunnerLifecycle

DOCKER_SOCKET = "/var/run/docker.sock"
pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not os.path.exists(DOCKER_SOCKET), reason="no Docker daemon socket at /var/run/docker.sock"
    ),
]

# A tiny, always-present image: the daemon pulls it once in CI.
TEST_IMAGE_REF = "busybox:1.37"


@pytest.fixture
async def client():
    async with DockerClient() as docker:
        yield docker


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / uuid.uuid4().hex[:8]
    (d / "workspace").mkdir(parents=True)
    (d / "outputs").mkdir()
    (d / "secrets").mkdir()
    (d / "task.json").write_text("{}")
    os.chmod(d, 0o777)
    os.chmod(d / "outputs", 0o777)
    return d


async def pinned_image(client: DockerClient) -> str:
    """Resolve the test image to a digest — create bodies are digest-only."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        TEST_IMAGE_REF,
        "--format",
        "{{index .RepoDigests 0}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0 or not out.strip():
        pull = await asyncio.create_subprocess_exec(
            "docker", "pull", "--quiet", TEST_IMAGE_REF, stdout=asyncio.subprocess.PIPE
        )
        await pull.communicate()
        return await pinned_image(client)
    return out.decode().strip()


def placement_for(run_dir, tag: str) -> RunPlacement:
    return RunPlacement(
        run_id=f"itest-{tag}",
        container_name=f"werft-itest-{tag}",
        network_name=f"werft-itest-{tag}-net",
        dns_ip="127.0.0.11",
        workspace_dir=str(run_dir / "workspace"),
        outputs_dir=str(run_dir / "outputs"),
        task_json_path=str(run_dir / "task.json"),
        secrets_dir=str(run_dir / "secrets"),
    )


async def test_full_lifecycle_and_die_event_exit_code(client, run_dir):
    tag = uuid.uuid4().hex[:8]
    placement = placement_for(run_dir, tag)
    config = ProjectRunnerConfig(
        image_digest=await pinned_image(client), memory_bytes=2 << 30, nano_cpus=1_000_000_000
    )
    container_id = None
    try:
        await RunnerLifecycle(client).prepare(placement)
        lifecycle = RunnerLifecycle(client, ceiling_seconds=60)
        watcher = asyncio.create_task(lifecycle.await_completion(placement, "pending"))
        await asyncio.sleep(0.2)
        watcher.cancel()

        container_id = await lifecycle.launch(
            placement, config, entrypoint=["/bin/sh", "-c", "exit 4"]
        )
        completion = await lifecycle.await_completion(placement, container_id)
        assert completion.exit_code == 4
        assert completion.meaning == "workspace_git_failure"
        assert completion.timed_out is False
    finally:
        await RunnerLifecycle(client).teardown(placement, container_id)


async def test_nothing_leaks_after_teardown(client, run_dir):
    tag = uuid.uuid4().hex[:8]
    placement = placement_for(run_dir, tag)
    config = ProjectRunnerConfig(
        image_digest=await pinned_image(client), memory_bytes=2 << 30, nano_cpus=1_000_000_000
    )
    lifecycle = RunnerLifecycle(client, ceiling_seconds=60)
    await lifecycle.prepare(placement)
    container_id = await lifecycle.launch(placement, config, entrypoint=["/bin/true"])
    await lifecycle.await_completion(placement, container_id)
    await lifecycle.teardown(placement, container_id)

    networks = [n["Name"] for n in await client.list_networks()]
    containers = [name for c in await client.list_containers() for name in c.get("Names", [])]
    assert placement.network_name not in networks
    assert not any(placement.container_name in name for name in containers)


async def test_two_concurrent_runs_cannot_reach_each_other(client, tmp_path):
    """Issue #20 acceptance: per-run networks isolate concurrent runs."""
    image = await pinned_image(client)
    config = ProjectRunnerConfig(image_digest=image, memory_bytes=2 << 30, nano_cpus=1_000_000_000)
    placements, ids = [], []
    for tag in ("a", "b"):
        d = tmp_path / "runs" / tag
        (d / "workspace").mkdir(parents=True)
        (d / "outputs").mkdir()
        (d / "secrets").mkdir()
        (d / "task.json").write_text("{}")
        placements.append(placement_for(d, f"{tag}-{uuid.uuid4().hex[:6]}"))

    lifecycle = RunnerLifecycle(client, ceiling_seconds=60)
    try:
        for placement in placements:
            await lifecycle.prepare(placement)
            ids.append(
                await lifecycle.launch(placement, config, entrypoint=["/bin/sh", "-c", "sleep 30"])
            )

        b_info = await client.inspect_container(ids[1])
        b_networks = b_info["NetworkSettings"]["Networks"]
        b_ip = next(iter(b_networks.values()))["IPAddress"]
        assert b_ip, "run B must have an address on its own network"

        probe = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            ids[0],
            "/bin/sh",
            "-c",
            f"nc -w 2 -z {b_ip} 22 || ping -c1 -W2 {b_ip}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert await probe.wait() != 0, "run A must not reach run B's address"
    finally:
        for placement, container_id in zip(placements, ids, strict=False):
            await lifecycle.teardown(placement, container_id)


async def test_ceiling_kills_a_runaway_container(client, run_dir):
    tag = uuid.uuid4().hex[:8]
    placement = placement_for(run_dir, tag)
    config = ProjectRunnerConfig(
        image_digest=await pinned_image(client), memory_bytes=2 << 30, nano_cpus=1_000_000_000
    )
    lifecycle = RunnerLifecycle(client, ceiling_seconds=5)
    container_id = None
    try:
        await lifecycle.prepare(placement)
        container_id = await lifecycle.launch(
            placement, config, entrypoint=["/bin/sh", "-c", "sleep 600"]
        )
        completion = await lifecycle.await_completion(placement, container_id)
        assert completion.timed_out is True
    finally:
        await RunnerLifecycle(client).teardown(placement, container_id)


async def test_symlink_attack_in_a_container_written_tree_yields_zero_bytes(
    client, run_dir, tmp_path
):
    """Issue #20 acceptance, end to end: the container plants the symlink."""
    tag = uuid.uuid4().hex[:8]
    placement = placement_for(run_dir, tag)
    config = ProjectRunnerConfig(
        image_digest=await pinned_image(client), memory_bytes=2 << 30, nano_cpus=1_000_000_000
    )
    lifecycle = RunnerLifecycle(client, ceiling_seconds=60)
    container_id = None
    try:
        await lifecycle.prepare(placement)
        container_id = await lifecycle.launch(
            placement,
            config,
            entrypoint=[
                "/bin/sh",
                "-c",
                "ln -s /etc/passwd /outputs/report.html; echo ok > /outputs/fine.txt",
            ],
        )
        await lifecycle.await_completion(placement, container_id)

        dest = tmp_path / "collected"
        report = collect_outputs(str(placement.outputs_dir), str(dest))

        assert [a.rel_path for a in report.artifacts] == ["fine.txt"]
        assert any(
            d.rel_path == "report.html" and d.reason == "not_regular" for d in report.dropped
        )
        assert not (dest / "report.html").exists()
    finally:
        await RunnerLifecycle(client).teardown(placement, container_id)
