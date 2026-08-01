"""The container-create body: a pure function of three enumerated components (SPEC §4.2).

Every field derives exclusively from Werft-owned state [CD M1]. No byte
originating in a managed repository or a run workspace is ever an input —
`runs.result` and `backlog_items.body` are agent-reachable, so this module takes
explicit **columns** (a typed placement and a typed config), never rows.

SPEC §4.2 governs this file over the lineage documents, which describe the
superseded non-root posture: here the box is capable — root inside,
`ReadonlyRootfs=false`, no `User` key — and separation is per-run network plus
the private `:Z` workspace label.
"""

import os
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Settled empirically 2026-08-01 against Docker CE 29.6.2 on rockylinux/rockylinux:10:
# root + CapDrop=ALL, one capability added per observed failure (scripts/capfloor.sh).
# SETFCAP is beyond SPEC §4.2's expected set — rpm restores file capabilities
# (httpd/suexec, dumpcap) and fails the whole transaction without it.
# NET_BIND_SERVICE is deliberately absent: Docker sets
# net.ipv4.ip_unprivileged_port_start=0, so services still bind ports below 1024.
CAP_ADD_FLOOR: tuple[str, ...] = (
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "KILL",
    "SETFCAP",
    "SETGID",
    "SETUID",
)

#: Never varies, never grantable; asserted by byte-equality in the body test.
BASE_HOST_CONFIG: dict[str, Any] = {
    "CapDrop": ["ALL"],
    "CapAdd": list(CAP_ADD_FLOOR),
    "SecurityOpt": ["no-new-privileges:true"],  # default seccomp = no entry at all
    "ReadonlyRootfs": False,  # SPEC §4.2 — the capable-box decision
    "Privileged": False,
    "PidsLimit": 4096,
    "ShmSize": 1 << 30,  # Chromium
    "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=1g"},
    "PortBindings": {},
    "AutoRemove": False,  # the manager removes explicitly, after reading the outputs
}

#: The per-run computed component. No other key may ever appear here (SPEC §4.2).
#: The container name is a query parameter on POST /containers/create, not a body
#: key, so it is not listed: lifecycle.py passes RunPlacement.container_name there.
PER_RUN_KEYS: frozenset[str] = frozenset({"NetworkMode", "Dns", "Binds", "Labels"})

#: The closed typed per-project delta — columns, not rows; ranges, not free values.
DELTA_KEYS: frozenset[str] = frozenset({"Memory", "NanoCpus"})


class CreateBodyError(Exception):
    """The body could not be built from Werft-owned state alone."""


class ProjectRunnerConfig(BaseModel):
    """Per-project delta. `extra="forbid"` is what makes the vocabulary closed:
    `privileged`, `capAdd`, `securityOpt`, `mounts` and friends are not expressible
    at any privilege level [CD M2].
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    image_digest: str
    memory_bytes: int = Field(ge=2 << 30, le=32 << 30)
    nano_cpus: int = Field(ge=1_000_000_000, le=8_000_000_000)


@dataclass(frozen=True)
class RunPlacement:
    """Enumerated per-run facts, every one manager-computed."""

    run_id: str
    container_name: str
    network_name: str
    dns_ip: str
    workspace_dir: str
    outputs_dir: str
    task_json_path: str
    secrets_dir: str


def _contained(path: str, run_root: str) -> str:
    """Resolve a mount source and refuse anything outside this run's directory."""
    real = os.path.realpath(path)
    if real != run_root and not real.startswith(run_root + os.sep):
        raise CreateBodyError(
            f"mount source {path!r} resolves outside the run directory {run_root!r}"
        )
    return real


def build_create_body(
    placement: RunPlacement, config: ProjectRunnerConfig, *, entrypoint: list[str]
) -> dict[str, Any]:
    """Compose BASE | per-run | delta into a POST /containers/create body."""
    if "@sha256:" not in config.image_digest:
        raise CreateBodyError(
            f"image {config.image_digest!r} must be digest-pinned (SPEC §4.1); tags are rejected"
        )

    run_root = os.path.dirname(os.path.realpath(placement.task_json_path))
    workspace = _contained(placement.workspace_dir, run_root)
    outputs = _contained(placement.outputs_dir, run_root)
    secrets = _contained(placement.secrets_dir, run_root)
    task_json = _contained(placement.task_json_path, run_root)

    host_config: dict[str, Any] = {
        key: (value.copy() if isinstance(value, dict | list) else value)
        for key, value in BASE_HOST_CONFIG.items()
    }
    host_config["Memory"] = config.memory_bytes
    host_config["NanoCpus"] = config.nano_cpus
    host_config["NetworkMode"] = placement.network_name
    host_config["Dns"] = [placement.dns_ip]
    host_config["Binds"] = [
        f"{workspace}:/work:rw,Z",  # private label — no other container may read it
        f"{outputs}:/outputs:rw,z",  # shared label — the manager reads it after exit (§4.3)
        f"{task_json}:/task.json:ro",
        f"{secrets}:/run/secrets:ro",  # a DIRECTORY, so token re-mint by rename is seen
    ]

    return {
        "Image": config.image_digest,
        "Entrypoint": list(entrypoint),
        "Cmd": [],
        "WorkingDir": "/work",
        "Labels": {"werft.run_id": placement.run_id},
        "HostConfig": host_config,
    }
