"""Claiming and releasing a run's egress slot (SPEC §4.5).

`runner/egress_admin.py` is the pure half — slot arithmetic and the on-disk
squid allowlist files. This module is the half that talks to the daemon, and it
lives in `orchestrator/` for one reason: it needs `Settings`, and it is shared
by the two components that own a run's network — `driver.py` on the dispatch
and teardown paths, `sweeps.py` on the crash-recovery one. `runner/` may not
import `orchestrator/`, so nothing here may ever be reached from there.

The slot table has no registry and no lock file. **Docker's own address-pool
refusal is the lock**: a slot is taken exactly when a network already holds its
/24, and claiming one is `create_network(name, subnet=slot_subnet(k))` walked
from k=0 until the daemon stops saying "pool overlaps". That is the only
arbitration that survives a manager crash, because it is a fact about the
daemon rather than about this process's memory.

Every failure here is **transient by construction** (`EgressUnavailable`). A
full slot table is capacity; an egress-proxy container that is not running is a
deployment fact; a read-only allowlist directory is a host fact. None of them
is the run's fault, so none of them may park it — they requeue it with backoff
and the next attempt finds a healthier host. The one deliberate exception is
the squid reload signal (D2): the allowlist *file* is the truth squid re-reads,
so a HUP that did not land is a log line, never a dispatch failure.
"""

from dataclasses import replace

import structlog

from werft.config.settings import Settings
from werft.runner import egress_admin
from werft.runner.create_body import RunPlacement
from werft.runner.docker_api import DockerApiError, DockerClient, is_pool_overlap
from werft.runner.lifecycle import RunnerLifecycle

logger = structlog.get_logger(__name__)


class EgressUnavailable(RuntimeError):
    """The run's egress slot could not be established. **Transient**: the
    driver's generic failure path requeues with backoff (never parks), which is
    the whole point of not raising `PermanentError` here."""


def egress_on(settings: Settings) -> bool:
    return settings.egress_slot_count > 0


def slot_of(settings: Settings, subnets: list[str]) -> int | None:
    """Which egress slot a run's network belongs to, from the daemon's own
    IPAM answer. `None` when egress is off, when the network is already gone
    (`subnets == []`), or when it was created before egress was switched on."""
    if not egress_on(settings):
        return None
    return egress_admin.slot_from_subnets(subnets, prefix=settings.egress_subnet_prefix)


def placement_with_slot(
    placement: RunPlacement, settings: Settings, slot: int | None
) -> RunPlacement:
    """The same placement with the slot's resolver and proxy filled in. `None`
    leaves it exactly as built — that *is* the egress-off placement."""
    if slot is None:
        return placement
    prefix = settings.egress_subnet_prefix
    squid = egress_admin.slot_squid_ip(slot, prefix=prefix)
    return replace(
        placement,
        dns_ip=egress_admin.slot_dns_ip(slot, prefix=prefix),
        proxy_url=f"http://{squid}:{settings.egress_proxy_port}",
    )


async def attach_egress(
    *,
    docker: DockerClient,
    lifecycle: RunnerLifecycle,
    settings: Settings,
    placement: RunPlacement,
    hosts: list[str],
    run_id: str,
) -> RunPlacement:
    """Create the run's network and, when egress is on, make it a slot.

    Off, this is exactly the create it has always been and the placement comes
    back untouched. On, it claims the first free slot, attaches the
    egress-proxy and dns-guard containers at their fixed addresses inside it,
    writes the slot's allowlist from `hosts`, asks squid to reload, and returns
    the placement carrying that slot's resolver and proxy URL.

    Everything after a successful create is wrapped: a half-attached network is
    worse than no network, so any failure gives the slot straight back before
    raising `EgressUnavailable`.
    """
    if not egress_on(settings):
        await lifecycle.prepare(placement)
        return placement

    slot = await _claim_slot(lifecycle, settings, placement, run_id)
    prefix = settings.egress_subnet_prefix
    try:
        await docker.connect_network(
            placement.network_name,
            settings.egress_proxy_container,
            ipv4=egress_admin.slot_squid_ip(slot, prefix=prefix),
        )
        await docker.connect_network(
            placement.network_name,
            settings.dns_guard_container,
            ipv4=egress_admin.slot_dns_ip(slot, prefix=prefix),
        )
        # OSError is deliberate in `write_allowlist` and deliberately caught
        # here: a run whose allowlist never landed would start with the
        # *previous* occupant's hosts, so it must not start at all.
        egress_admin.write_allowlist(settings.egress_allowlist_dir, slot, hosts)
    except Exception as exc:  # noqa: BLE001 - every one of them is transient
        logger.warning(
            "egress.attach_failed",
            run_id=run_id,
            slot=slot,
            error=str(exc),
            network=placement.network_name,
        )
        await _release(
            docker, settings, network_name=placement.network_name, slot=slot, run_id=run_id
        )
        raise EgressUnavailable(f"egress slot {slot} could not be attached: {exc}") from exc

    await _reload_squid(docker, settings, run_id)
    logger.info("egress.slot_claimed", run_id=run_id, slot=slot, hosts=len(hosts))
    return placement_with_slot(placement, settings, slot)


async def detach_egress(
    docker: DockerClient,
    settings: Settings,
    *,
    network_name: str,
    subnets: list[str],
    run_id: str,
) -> None:
    """Give a run's egress slot back, from the network's own subnets.

    Called between container removal and network removal on both teardown
    paths — the driver's and the sweep's — so the two behave identically. A
    no-op when no slot is derivable, which is what makes the egress-off path
    byte-for-byte what it was. Never raises: this runs while other failures may
    already be in flight, and a detach that could not finish must still leave
    the network removal to run.
    """
    slot = slot_of(settings, subnets)
    if slot is None:
        return
    try:
        egress_admin.clear_allowlist(settings.egress_allowlist_dir, slot)
    except OSError as exc:
        # Best effort by ruling: the slot's next occupant rewrites this file
        # before its container exists, so a stale file is never *reachable*.
        logger.warning("egress.allowlist_clear_failed", run_id=run_id, slot=slot, error=str(exc))
    await _reload_squid(docker, settings, run_id)
    await _release(docker, settings, network_name=network_name, slot=slot, run_id=run_id)


async def _claim_slot(
    lifecycle: RunnerLifecycle, settings: Settings, placement: RunPlacement, run_id: str
) -> int:
    """The first slot whose /24 the daemon will hand over, walked from 0."""
    prefix = settings.egress_subnet_prefix
    for slot in range(settings.egress_slot_count):
        try:
            await lifecycle.prepare(placement, subnet=egress_admin.slot_subnet(slot, prefix=prefix))
        except DockerApiError as exc:
            if is_pool_overlap(exc):
                logger.debug("egress.slot_taken", run_id=run_id, slot=slot)
                continue
            raise EgressUnavailable(f"egress slot {slot} could not be created: {exc}") from exc
        return slot
    raise EgressUnavailable(f"all {settings.egress_slot_count} egress slots are in use")


async def _release(
    docker: DockerClient, settings: Settings, *, network_name: str, slot: int, run_id: str
) -> None:
    """Detach both service containers from a slot's network, tolerating every
    "already in that state" answer `docker_api.disconnect_network` swallows."""
    for container in (settings.egress_proxy_container, settings.dns_guard_container):
        try:
            await docker.disconnect_network(network_name, container, force=True)
        except Exception as exc:  # noqa: BLE001 - the network removal still owes a try
            logger.warning(
                "egress.disconnect_failed",
                run_id=run_id,
                slot=slot,
                container=container,
                error=str(exc),
            )


async def _reload_squid(docker: DockerClient, settings: Settings, run_id: str) -> None:
    """D2: the file is the truth, the signal is the optimisation."""
    try:
        await docker.kill_container(settings.egress_proxy_container, signal="HUP")
    except Exception as exc:  # noqa: BLE001 - never fails a run
        logger.warning("egress.allowlist_reload_failed", run_id=run_id, error=str(exc))
