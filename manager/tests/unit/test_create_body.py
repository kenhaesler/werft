"""SPEC §4.2: the create-body is built from three components and the body test
asserts byte-equality on BASE, key-enumeration on the per-run component, schema
on the delta, and explicit negative assertions.
"""

import pytest
from pydantic import ValidationError

from werft.runner.create_body import (
    BASE_HOST_CONFIG,
    CAP_ADD_FLOOR,
    DELTA_KEYS,
    PER_RUN_KEYS,
    CreateBodyError,
    ProjectRunnerConfig,
    RunPlacement,
    build_create_body,
)

DIGEST = "werft-runner-elastic@sha256:" + "a" * 64
ENTRYPOINT = ["/opt/werft/adapter/bin/werft-adapter"]


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "r1"
    (d / "workspace").mkdir(parents=True)
    (d / "outputs").mkdir()
    (d / "secrets").mkdir()
    (d / "task.json").write_text("{}")
    return d


def placement(run_dir, **over):
    base = {
        "run_id": "0198e1b2-0000-7000-8000-000000000001",
        "container_name": "werft-run-0198e1b2",
        "network_name": "werft-run-0198e1b2-net",
        "dns_ip": "10.90.7.53",
        "workspace_dir": str(run_dir / "workspace"),
        "outputs_dir": str(run_dir / "outputs"),
        "task_json_path": str(run_dir / "task.json"),
        "secrets_dir": str(run_dir / "secrets"),
    }
    return RunPlacement(**(base | over))


def config(**over):
    base = {"image_digest": DIGEST, "memory_bytes": 8 << 30, "nano_cpus": 4_000_000_000}
    return ProjectRunnerConfig(**(base | over))


def body(run_dir, **over):
    return build_create_body(placement(run_dir, **over), config(), entrypoint=ENTRYPOINT)


def test_base_host_config_is_byte_equal(run_dir):
    host = body(run_dir)["HostConfig"]
    for key, value in BASE_HOST_CONFIG.items():
        assert host[key] == value, f"BASE key {key} was mutated"


def test_cap_floor_is_the_empirically_locked_set(run_dir):
    # SPEC §4.2 defers the exact floor to an empirical test "and then locked by
    # the create-body test". Settled 2026-08-01 — see scripts/capfloor.sh.
    assert CAP_ADD_FLOOR == (
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "KILL",
        "SETFCAP",
        "SETGID",
        "SETUID",
    )
    assert body(run_dir)["HostConfig"]["CapAdd"] == list(CAP_ADD_FLOOR)


def test_per_run_component_keys_are_exactly_enumerated():
    """SPEC §4.2: no other key may ever appear in the per-run component."""
    assert frozenset({"NetworkMode", "Dns", "Binds", "Labels"}) == PER_RUN_KEYS


def test_delta_keys_are_exactly_enumerated():
    assert frozenset({"Memory", "NanoCpus"}) == DELTA_KEYS


def test_host_config_has_no_key_outside_the_three_components(run_dir):
    host = body(run_dir)["HostConfig"]
    unexpected = set(host) - set(BASE_HOST_CONFIG) - PER_RUN_KEYS - DELTA_KEYS
    assert unexpected == set(), f"unenumerated HostConfig keys: {unexpected}"


def test_negative_assertions_on_the_final_body(run_dir):
    b = body(run_dir)
    host = b["HostConfig"]
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    for never in ("SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "NET_ADMIN", "NET_RAW"):
        assert never not in host["CapAdd"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    for opt in host["SecurityOpt"]:
        assert "unconfined" not in opt
        assert "label=disable" not in opt
    for absent in (
        "Devices",
        "DeviceCgroupRules",
        "VolumesFrom",
        "Runtime",
        "CgroupParent",
        "Sysctls",
        "ExtraHosts",
        "PidMode",
        "IpcMode",
        "CgroupnsMode",
        "UsernsMode",
        "NetworkMode_host",
    ):
        assert absent not in host, f"{absent} must be ABSENT, not falsy"
    assert host["PortBindings"] == {}
    assert "User" not in b, "SPEC §4.2: root inside — User absent"
    assert host["ReadonlyRootfs"] is False, "SPEC §4.2: capable box"
    assert host["NetworkMode"] != "host"
    assert all("docker.sock" not in bind for bind in host["Binds"])


def test_spec_resource_shape(run_dir):
    host = body(run_dir)["HostConfig"]
    assert host["PidsLimit"] == 4096
    assert host["ShmSize"] == 1 << 30
    assert host["Tmpfs"] == {"/tmp": "rw,nosuid,nodev,size=1g"}


def test_mount_shape_matches_spec_verbatim(run_dir):
    """SPEC §4.2: workspace rw :Z; outputs dir rw :z; task.json ro; secrets/ ro; nothing else."""
    host = body(run_dir)["HostConfig"]
    assert len(host["Binds"]) == 4
    workspace, outputs, task, secrets = host["Binds"]
    assert workspace.endswith(":/work:rw,Z")  # private label
    assert outputs.endswith(":/outputs:rw,z")  # shared label — manager reads after exit (§4.3)
    assert task.endswith(":/task.json:ro")
    assert secrets.endswith(":/run/secrets:ro")  # DIRECTORY — re-mint by rename is seen


def test_top_level_body_keys_are_exactly_enumerated(run_dir):
    """Nothing may appear at the top level beyond the enumerated set — a new key
    there (User, NetworkingConfig, HostConfig-bypassing fields) must fail loudly."""
    assert set(body(run_dir)) == {
        "Image",
        "Entrypoint",
        "Cmd",
        "WorkingDir",
        "Labels",
        "HostConfig",
    }


def test_labels_carry_the_run_id_for_the_events_filter(run_dir):
    assert body(run_dir)["Labels"] == {"werft.run_id": "0198e1b2-0000-7000-8000-000000000001"}


def test_image_must_be_digest_pinned(run_dir):
    with pytest.raises(CreateBodyError, match="digest"):
        build_create_body(
            placement(run_dir),
            config(image_digest="werft-runner-elastic:latest"),
            entrypoint=ENTRYPOINT,
        )


def test_config_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        ProjectRunnerConfig(
            image_digest=DIGEST, memory_bytes=8 << 30, nano_cpus=1_000_000_000, privileged=True
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("memory_bytes", 1 << 20),
        ("memory_bytes", 1 << 50),
        ("nano_cpus", 0),
        ("nano_cpus", 99_000_000_000),
    ],
)
def test_resource_values_must_be_in_range(field, value):
    with pytest.raises(ValidationError):
        config(**{field: value})


def test_mount_source_outside_the_run_directory_is_refused(run_dir, tmp_path):
    other = tmp_path / "runs" / "r2" / "workspace"
    other.mkdir(parents=True)
    with pytest.raises(CreateBodyError, match="outside"):
        build_create_body(
            placement(run_dir, workspace_dir=str(other)), config(), entrypoint=ENTRYPOINT
        )


def test_mount_source_traversal_is_refused(run_dir):
    escape = str(run_dir / ".." / ".." / "etc")
    with pytest.raises(CreateBodyError, match="outside"):
        build_create_body(placement(run_dir, workspace_dir=escape), config(), entrypoint=ENTRYPOINT)


def test_symlinked_mount_source_is_refused(run_dir, tmp_path):
    """realpath, not the literal path — a symlink out of the run dir must not pass."""
    secret = tmp_path / "host-secrets"
    secret.mkdir()
    link = run_dir / "workspace-link"
    try:
        link.symlink_to(secret, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this platform")
    with pytest.raises(CreateBodyError, match="outside"):
        build_create_body(
            placement(run_dir, workspace_dir=str(link)), config(), entrypoint=ENTRYPOINT
        )


def test_property_no_representable_delta_can_reach_a_forbidden_key(run_dir):
    """Over the whole representable grant space, BASE and the negatives still hold."""
    for mem in (2 << 30, 8 << 30, 32 << 30):
        for cpus in (1_000_000_000, 4_000_000_000, 8_000_000_000):
            b = build_create_body(
                placement(run_dir),
                config(memory_bytes=mem, nano_cpus=cpus),
                entrypoint=ENTRYPOINT,
            )
            host = b["HostConfig"]
            for key, value in BASE_HOST_CONFIG.items():
                assert host[key] == value
            assert host["Privileged"] is False
            assert "User" not in b
            assert set(host) - set(BASE_HOST_CONFIG) - PER_RUN_KEYS - DELTA_KEYS == set()


def test_base_is_not_mutated_by_building_a_body(run_dir):
    """A shared-dict aliasing bug would let one run's delta leak into the next."""
    before = {
        k: (v.copy() if isinstance(v, dict | list) else v) for k, v in BASE_HOST_CONFIG.items()
    }
    build_create_body(placement(run_dir), config(memory_bytes=32 << 30), entrypoint=ENTRYPOINT)
    build_create_body(placement(run_dir), config(memory_bytes=2 << 30), entrypoint=ENTRYPOINT)
    assert before == BASE_HOST_CONFIG
    assert "Memory" not in BASE_HOST_CONFIG


def test_binds_list_is_not_aliased_between_bodies(run_dir):
    a = build_create_body(placement(run_dir), config(), entrypoint=ENTRYPOINT)
    b = build_create_body(placement(run_dir), config(), entrypoint=ENTRYPOINT)
    a["HostConfig"]["Binds"].append("/evil:/evil:rw")
    assert len(b["HostConfig"]["Binds"]) == 4
