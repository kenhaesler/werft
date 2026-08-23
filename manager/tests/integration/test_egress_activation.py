"""Egress activation, end to end through the real driver and the real sweeps
(SPEC §4.5; T9 task 6).

The contract is behavioural, so these tests drive `attend_run` and
`reap_run_containers` themselves and read the fake daemon's record afterwards:
which subnet a run's network claimed, which service containers were attached at
which addresses, what landed in the slot's squid allowlist file, and what was
given back at teardown.

Everything else is `test_driver.py`'s harness, imported wholesale — an egress
test that drifted onto its own fixtures would stop proving anything about the
driver the rest of the suite exercises. `egress_slot_count` defaults to 0, so
the fixture arrives egress-off and each test opts in through `enable()`.
"""

import asyncio
import json
from pathlib import Path

import pytest

from tests.fakes import write_outputs
from tests.integration import test_driver
from tests.integration.test_driver import (
    DIGEST,
    fetch,
    latest_attempt,
    network_of,
    placement_of,
    set_running_with_container,
    sweep_deps_of,
)
from werft.domain.runs import run_branch_name
from werft.orchestrator.driver import attend_run
from werft.orchestrator.sweeps import reap_run_containers
from werft.runner.egress_admin import BASE_ALLOW, write_allowlist

#: `test_driver`'s harness, re-bound so pytest registers the same fixtures here
#: — a real Postgres row, a real git origin, a claimed run and the fake daemon.
#: Rebinding rather than re-declaring is the point: an egress test that grew its
#: own fixtures would stop proving anything about the driver everything else
#: exercises.
origin = test_driver.origin
deps_fixture = test_driver.deps_fixture

PROXY = "werft-egress-proxy"
DNS_GUARD = "werft-dns-guard"


def enable(fakes, tmp_path, *, slots: int = 4) -> str:
    """Turn egress activation on for this run's settings and return the
    allowlist directory the driver will write into."""
    allow_dir = str(tmp_path / "egress-allow")
    fakes.settings.egress_slot_count = slots
    fakes.settings.egress_allowlist_dir = allow_dir
    return allow_dir


def configure_hosts(tmp_path, slug: str, *, registries: list[str], extra_hosts: list[str]) -> None:
    """Rewrite the dispatch config the fixture wrote, adding the project's
    declared egress hosts. `DispatchConfigCache.current()` re-reads on every
    call, so this lands even though the cache already exists."""
    (tmp_path / "dispatch.json").write_text(
        json.dumps(
            {
                "projects": {
                    slug: {
                        "image_digest": DIGEST,
                        "model": "claude-sonnet-4-6",
                        "timeout_seconds": 1800,
                        "registries": registries,
                        "extra_hosts": extra_hosts,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def allowlist_text(allow_dir: str, slot: int) -> str:
    return Path(allow_dir, f"slot{slot}.txt").read_text(encoding="utf-8")


def subnets_of_calls(fakes) -> list[str | None]:
    return [subnet for _name, subnet in fakes.docker.created_networks]


# --- 1: off ------------------------------------------------------------------


async def test_egress_off_creates_the_network_exactly_as_before(deps_fixture, tmp_path):
    """`egress_slot_count == 0` is today's behaviour byte for byte: no IPAM
    pool on the create, no service containers attached, no allowlist file, no
    reload signal, and the off-mode placement values in the create body."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.on_started = lambda p: write_outputs(p, status="success")

    await attend_run(driver_deps, run_id)

    assert fakes.docker.created_networks == [(network_of(run_id), None)]
    assert fakes.docker.connected == []
    assert fakes.docker.disconnected == []
    assert not any(signal == "HUP" for _c, signal in fakes.docker.kill_signals)
    assert not (tmp_path / "egress-allow").exists()
    body = fakes.docker.created_bodies[0]
    assert "Env" not in body
    assert body["HostConfig"]["Dns"] == [fakes.settings.runner_dns_ip]


# --- 2: claiming a slot ------------------------------------------------------


async def test_the_first_free_slot_is_claimed(deps_fixture, tmp_path):
    driver_deps, run_id, fakes = deps_fixture
    enable(fakes, tmp_path)
    fakes.on_started = lambda p: write_outputs(p, status="success")

    await attend_run(driver_deps, run_id)

    assert subnets_of_calls(fakes) == ["10.90.0.0/24"]


async def test_a_held_slot_is_skipped_for_the_next_one(deps_fixture, tmp_path):
    """Docker's own pool-overlap refusal is the lock: slot 0 belongs to a live
    run's network, so this run takes slot 1 and attaches there."""
    driver_deps, run_id, fakes = deps_fixture
    allow_dir = enable(fakes, tmp_path)
    fakes.docker.held_subnets["10.90.0.0/24"] = "werft-net-someone-else"
    fakes.on_started = lambda p: write_outputs(p, status="success")

    await attend_run(driver_deps, run_id)

    assert subnets_of_calls(fakes) == ["10.90.0.0/24", "10.90.1.0/24"]
    assert (network_of(run_id), PROXY, "10.90.1.2") in fakes.docker.connected
    assert (network_of(run_id), DNS_GUARD, "10.90.1.3") in fakes.docker.connected
    assert Path(allow_dir, "slot1.txt").exists()


async def test_every_slot_taken_requeues_the_run_rather_than_parking_it(deps_fixture, tmp_path):
    """A full slot table is a capacity fact, never the run's fault: back to the
    queue with backoff, and the network of the last attempt is not left behind."""
    driver_deps, run_id, fakes = deps_fixture
    enable(fakes, tmp_path, slots=2)
    fakes.docker.held_subnets = {
        "10.90.0.0/24": "werft-net-a",
        "10.90.1.0/24": "werft-net-b",
    }

    await attend_run(driver_deps, run_id)  # must not raise

    row = await fetch(fakes, run_id)
    assert row.status == "queued" and row.parked_reason is None
    assert row.next_attempt_at is not None
    assert (await latest_attempt(fakes, run_id)).outcome == "infra_failure"
    assert subnets_of_calls(fakes) == ["10.90.0.0/24", "10.90.1.0/24"]
    assert fakes.docker.created_bodies == []  # no container was ever created


async def test_a_failed_attach_removes_the_network_and_requeues(deps_fixture, tmp_path):
    """A missing egress-proxy container must never park a run: the just-created
    network is given back and the run returns to the queue."""
    driver_deps, run_id, fakes = deps_fixture
    enable(fakes, tmp_path)
    fakes.docker.raise_on = "connect_network"

    await attend_run(driver_deps, run_id)  # must not raise

    row = await fetch(fakes, run_id)
    assert row.status == "queued" and row.parked_reason is None
    assert f"remove_network:{network_of(run_id)}" in fakes.docker.calls
    assert fakes.docker.created_bodies == []


# --- allowlist + reload ------------------------------------------------------


async def test_the_allowlist_carries_the_presets_the_extras_and_the_base(deps_fixture, tmp_path):
    """Read *during* the run: teardown clears the file on the way out, which is
    its own test below."""
    driver_deps, run_id, fakes = deps_fixture
    allow_dir = enable(fakes, tmp_path)
    configure_hosts(tmp_path, fakes.slug, registries=["pypi"], extra_hosts=["Example.COM"])
    seen: dict[str, str] = {}

    def on_started(placement):
        seen["text"] = allowlist_text(allow_dir, 0)
        write_outputs(placement, status="success")

    fakes.on_started = on_started

    await attend_run(driver_deps, run_id)

    lines = seen["text"].splitlines()
    assert ".pypi.org" in lines and ".files.pythonhosted.org" in lines  # preset expanded
    assert ".example.com" in lines  # extra host, lowercased
    assert all(f".{host}" in lines for host in BASE_ALLOW)
    assert lines == sorted(lines)


async def test_squid_is_hupped_after_the_allowlist_is_written(deps_fixture, tmp_path):
    driver_deps, run_id, fakes = deps_fixture
    enable(fakes, tmp_path)
    hups: list[list[tuple[str, str]]] = []
    fakes.on_started = lambda p: (hups.append(list(fakes.docker.kill_signals)), write_outputs(p))

    await attend_run(driver_deps, run_id)

    assert (PROXY, "HUP") in hups[0]


async def test_a_failed_hup_does_not_fail_the_run(deps_fixture, tmp_path):
    """D2: the allowlist file is the truth; a reload that did not land is a log
    line, not a dispatch failure."""
    driver_deps, run_id, fakes = deps_fixture
    enable(fakes, tmp_path)
    fakes.docker.error = RuntimeError("squid is not listening")
    fakes.docker.failing_ops = {"kill_container"}
    fakes.on_started = lambda p: write_outputs(p, status="success")
    fakes.ops.ref_shas[run_branch_name(run_id)] = "f" * 40  # the agent pushed

    await attend_run(driver_deps, run_id)  # must not raise

    assert (await fetch(fakes, run_id)).status == "awaiting_review"


# --- placement ---------------------------------------------------------------


async def test_the_create_body_carries_the_slot_proxy_and_dns(deps_fixture, tmp_path):
    driver_deps, run_id, fakes = deps_fixture
    enable(fakes, tmp_path)
    fakes.on_started = lambda p: write_outputs(p, status="success")

    await attend_run(driver_deps, run_id)

    body = fakes.docker.created_bodies[0]
    assert body["HostConfig"]["Dns"] == ["10.90.0.3"]
    assert "HTTPS_PROXY=http://10.90.0.2:3128" in body["Env"]
    assert "https_proxy=http://10.90.0.2:3128" in body["Env"]


# --- teardown ----------------------------------------------------------------


async def test_teardown_clears_the_allowlist_and_gives_the_slot_back(deps_fixture, tmp_path):
    driver_deps, run_id, fakes = deps_fixture
    allow_dir = enable(fakes, tmp_path)
    fakes.on_started = lambda p: write_outputs(p, status="success")
    fakes.docker.network_body = {"IPAM": {"Config": [{"Subnet": "10.90.0.0/24"}]}}

    await attend_run(driver_deps, run_id)

    assert allowlist_text(allow_dir, 0) == ""
    assert (network_of(run_id), PROXY, True) in fakes.docker.disconnected
    assert (network_of(run_id), DNS_GUARD, True) in fakes.docker.disconnected
    order = fakes.docker.calls
    assert order.index(f"disconnect:{network_of(run_id)}:{DNS_GUARD}") < order.index(
        f"remove_network:{network_of(run_id)}"
    )


async def test_the_sweep_tears_a_dead_runs_slot_down_the_same_way(deps_fixture, tmp_path):
    """Crash recovery is the *normal* path, so the sweep owes the identical
    sequence: clear, reload, detach both services, remove the network."""
    driver_deps, run_id, fakes = deps_fixture
    allow_dir = enable(fakes, tmp_path)

    write_allowlist(allow_dir, 2, ["example.com"])
    await set_running_with_container(fakes, run_id, fakes.docker.container_id)
    fakes.docker.network_body = {"IPAM": {"Config": [{"Subnet": "10.90.2.0/24"}]}}

    ok = await reap_run_containers(sweep_deps_of(driver_deps, fakes), run_id, None)

    assert ok
    assert allowlist_text(allow_dir, 2) == ""
    assert (network_of(run_id), PROXY, True) in fakes.docker.disconnected
    assert (network_of(run_id), DNS_GUARD, True) in fakes.docker.disconnected
    assert (PROXY, "HUP") in fakes.docker.kill_signals
    order = fakes.docker.calls
    assert order.index(f"disconnect:{network_of(run_id)}:{PROXY}") < order.index(
        f"remove_network:{network_of(run_id)}"
    )


async def test_the_sweep_leaves_an_egress_off_run_alone(deps_fixture):
    """Off-mode parity for the sweep: nothing to clear, nothing to detach."""
    driver_deps, run_id, fakes = deps_fixture
    await set_running_with_container(fakes, run_id, fakes.docker.container_id)

    await reap_run_containers(sweep_deps_of(driver_deps, fakes), run_id, None)

    assert fakes.docker.disconnected == []
    assert fakes.docker.kill_signals == []


# --- re-adoption -------------------------------------------------------------


async def test_re_adoption_re_derives_the_slot_from_the_networks_subnet(deps_fixture, tmp_path):
    """The manager died; the row and the network are all that is left. The slot
    comes back out of the network's own IPAM pool — and the allowlist file,
    already this run's, is not rewritten."""
    driver_deps, run_id, fakes = deps_fixture
    allow_dir = enable(fakes, tmp_path)

    write_allowlist(allow_dir, 2, ["kept.example.com"])
    before = allowlist_text(allow_dir, 2)
    await set_running_with_container(fakes, run_id, fakes.docker.container_id)
    write_outputs(placement_of(fakes, run_id), status="success")
    fakes.docker.network_body = {"IPAM": {"Config": [{"Subnet": "10.90.2.0/24"}]}}
    fakes.docker.hold_die = True

    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await asyncio.sleep(0.2)
    assert allowlist_text(allow_dir, 2) == before  # no rewrite while attending
    fakes.docker.release_die(0)
    await task

    assert fakes.docker.created_networks == []  # re-adoption creates nothing
    assert fakes.docker.connected == []  # and re-attaches nothing
    assert allowlist_text(allow_dir, 2) == ""  # but teardown still gives it back


@pytest.mark.parametrize("body", [None, {"IPAM": {"Config": [{"Subnet": "172.30.0.0/24"}]}}])
async def test_re_adoption_without_a_derivable_slot_falls_back_to_off_mode(
    deps_fixture, tmp_path, body
):
    """Egress was off when this run was claimed (or the network is already
    gone): there is no slot to speak of, and teardown must not invent one."""
    driver_deps, run_id, fakes = deps_fixture
    enable(fakes, tmp_path)
    await set_running_with_container(fakes, run_id, fakes.docker.container_id)
    write_outputs(placement_of(fakes, run_id), status="success")
    fakes.docker.network_body = body
    fakes.docker.release_die(0)  # re-adoption never starts a container

    await attend_run(driver_deps, run_id)

    assert fakes.docker.disconnected == []
    assert not any(signal == "HUP" for _c, signal in fakes.docker.kill_signals)
