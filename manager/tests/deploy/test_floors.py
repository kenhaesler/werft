"""Task 13 (T9): install floors fail loudly.

`deploy/install/floors.sh` defines one `assert_*` function per floor (SPEC
§2, §10). Each is standalone and stub-testable: it consults only `$PATH`
commands and readable files (BUILD-PLAN P0.2). These tests build a temp
`bin/` directory of tiny `#!/bin/sh` stub commands per case, prepend it to
`PATH`, and run `bash -c 'source floors.sh && assert_<x>'` as a subprocess
so each assertion executes exactly as it would on a real host — no Python
reimplementation of the bash logic to drift from the source of truth.

For every floor: at least one satisfied case (rc 0, empty stderr) and one
violated case (rc != 0, stderr containing `FLOOR FAIL <name>: <reason>`).
Version floors get boundary cases per the C1/D13 pins in floors.sh's header
(29.6.2 docker floor, 29.4.2 refused-trap release, containerd 2.2.6, kernel
6.12.0-124.55.1.el10_1) plus a `sort -V` (not lexical) sanity case.

These tests exercise bash and Linux-only commands (`uname -r`,
`getenforce`, `findmnt`, `firewall-cmd`) — they run in Linux CI (Task 15)
and skip everywhere else, including this Windows dev box.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="floors.sh sources bash and shells out to Linux-only commands "
    "(uname -r, getenforce, findmnt, firewall-cmd); runs in Linux CI",
)

# manager/tests/deploy/test_floors.py -> repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]
FLOORS_SH = REPO_ROOT / "deploy" / "install" / "floors.sh"


def _write_stub(bin_dir: Path, name: str, body: str) -> Path:
    """Write an executable #!/bin/sh stub named `name` in `bin_dir`."""
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run(
    func: str, bin_dir: Path, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source floors.sh and invoke `func` in a bash subprocess with
    `bin_dir` prepended to PATH."""
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", f"source '{FLOORS_SH}' && {func}"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _assert_pass(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def _assert_fail(result: subprocess.CompletedProcess[str], name: str) -> None:
    assert result.returncode != 0
    assert f"FLOOR FAIL {name}:" in result.stderr


# ---------------------------------------------------------------------------
# x86_64_v3 (WERFT_LDSO indirection added in Task 12)
# ---------------------------------------------------------------------------


def test_x86_64_v3_satisfied(tmp_path: Path) -> None:
    ldso = _write_stub(
        tmp_path,
        "ldso-stub",
        "echo 'x86-64 (supported, required)'\n"
        "echo 'x86-64-v2 (supported, required)'\n"
        "echo 'x86-64-v3 (supported, required)'\n"
        "echo 'x86-64-v4 (unsupported by CPU)'\n",
    )
    result = _run("assert_x86_64_v3", tmp_path, {"WERFT_LDSO": str(ldso)})
    _assert_pass(result)


def test_x86_64_v3_violated_unsupported(tmp_path: Path) -> None:
    ldso = _write_stub(
        tmp_path,
        "ldso-stub",
        "echo 'x86-64 (supported, required)'\n"
        "echo 'x86-64-v2 (supported, required)'\n"
        "echo 'x86-64-v3 (unsupported by CPU)'\n",
    )
    result = _run("assert_x86_64_v3", tmp_path, {"WERFT_LDSO": str(ldso)})
    _assert_fail(result, "x86_64_v3")


def test_x86_64_v3_violated_ldso_missing_line(tmp_path: Path) -> None:
    """--help output that never mentions x86-64-v3 at all (older glibc)."""
    ldso = _write_stub(
        tmp_path,
        "ldso-stub",
        "echo 'x86-64 (supported, required)'\necho 'x86-64-v2 (supported, required)'\n",
    )
    result = _run("assert_x86_64_v3", tmp_path, {"WERFT_LDSO": str(ldso)})
    _assert_fail(result, "x86_64_v3")


# ---------------------------------------------------------------------------
# docker_version (C1: floor 29.6.2, 29.4.2 refused outright)
# ---------------------------------------------------------------------------


def test_docker_version_satisfied_at_floor(tmp_path: Path) -> None:
    _write_stub(tmp_path, "docker", "echo '29.6.2'\n")
    _assert_pass(_run("assert_docker_version", tmp_path))


def test_docker_version_satisfied_above_floor_sort_v_not_lexical(tmp_path: Path) -> None:
    """29.10.0 > 29.6.2 numerically; a lexical compare would wrongly fail
    this ('1' < '6' at the first differing digit)."""
    _write_stub(tmp_path, "docker", "echo '29.10.0'\n")
    _assert_pass(_run("assert_docker_version", tmp_path))


def test_docker_version_violated_just_below_floor(tmp_path: Path) -> None:
    _write_stub(tmp_path, "docker", "echo '29.6.1'\n")
    _assert_fail(_run("assert_docker_version", tmp_path), "docker_version")


def test_docker_version_violated_refused_trap_release(tmp_path: Path) -> None:
    """29.4.2 is refused outright even though a naive floor check on some
    other metric might let it through -- it's explicitly denylisted."""
    result = _run(
        "assert_docker_version", _write_stub(tmp_path, "docker", "echo '29.4.2'\n").parent
    )
    assert result.returncode != 0
    assert "FLOOR FAIL docker_version:" in result.stderr
    assert "refused" in result.stderr


def test_docker_version_violated_daemon_unreachable(tmp_path: Path) -> None:
    _write_stub(tmp_path, "docker", "exit 1\n")
    result = _run("assert_docker_version", tmp_path)
    _assert_fail(result, "docker_version")
    assert "unreachable" in result.stderr


# ---------------------------------------------------------------------------
# containerd_version (D13: floor 2.2.6, bundled with Docker CE 29.6.2)
# ---------------------------------------------------------------------------


def test_containerd_version_satisfied_at_floor(tmp_path: Path) -> None:
    _write_stub(
        tmp_path, "containerd", "echo 'containerd github.com/containerd/containerd v2.2.6 abc123'\n"
    )
    _assert_pass(_run("assert_containerd_version", tmp_path))


def test_containerd_version_satisfied_above_floor(tmp_path: Path) -> None:
    _write_stub(
        tmp_path, "containerd", "echo 'containerd github.com/containerd/containerd v2.3.0 abc123'\n"
    )
    _assert_pass(_run("assert_containerd_version", tmp_path))


def test_containerd_version_violated_below_floor(tmp_path: Path) -> None:
    _write_stub(
        tmp_path, "containerd", "echo 'containerd github.com/containerd/containerd v2.2.5 abc123'\n"
    )
    _assert_fail(_run("assert_containerd_version", tmp_path), "containerd")


def test_containerd_version_violated_absent(tmp_path: Path) -> None:
    """No containerd on PATH at all: the `command -v` gate must fire before
    the `_vergte "" ...` fallthrough that would print a misleading message."""
    result = _run("assert_containerd_version", tmp_path)
    _assert_fail(result, "containerd")
    assert "not found" in result.stderr


# ---------------------------------------------------------------------------
# kernel (D13: floor 6.12.0-124.55.1.el10_1, RHSA-2026:13566 / CVE-2026-31431)
# ---------------------------------------------------------------------------


def test_kernel_floor_satisfied_at_floor(tmp_path: Path) -> None:
    _write_stub(tmp_path, "uname", "echo '6.12.0-124.55.1.el10_1'\n")
    _assert_pass(_run("assert_kernel_floor", tmp_path))


def test_kernel_floor_satisfied_above_floor(tmp_path: Path) -> None:
    _write_stub(tmp_path, "uname", "echo '6.12.0-125.0.0.el10_1'\n")
    _assert_pass(_run("assert_kernel_floor", tmp_path))


def test_kernel_floor_violated_below_floor(tmp_path: Path) -> None:
    _write_stub(tmp_path, "uname", "echo '6.12.0-124.55.0.el10_1'\n")
    _assert_fail(_run("assert_kernel_floor", tmp_path), "kernel")


# ---------------------------------------------------------------------------
# selinux_enforcing
# ---------------------------------------------------------------------------


def test_selinux_enforcing_satisfied(tmp_path: Path) -> None:
    _write_stub(tmp_path, "getenforce", "echo 'Enforcing'\n")
    _assert_pass(_run("assert_selinux_enforcing", tmp_path))


def test_selinux_enforcing_violated_permissive(tmp_path: Path) -> None:
    _write_stub(tmp_path, "getenforce", "echo 'Permissive'\n")
    _assert_fail(_run("assert_selinux_enforcing", tmp_path), "selinux")


def test_selinux_enforcing_violated_missing(tmp_path: Path) -> None:
    """getenforce not installed: `2>/dev/null` output is empty, != Enforcing."""
    _assert_fail(_run("assert_selinux_enforcing", tmp_path), "selinux")


# ---------------------------------------------------------------------------
# docker_selinux_enabled
# ---------------------------------------------------------------------------


def test_docker_selinux_enabled_satisfied(tmp_path: Path) -> None:
    _write_stub(tmp_path, "docker", "echo '[\"selinux\"]'\n")
    _assert_pass(_run("assert_docker_selinux_enabled", tmp_path))


def test_docker_selinux_enabled_violated_missing_option(tmp_path: Path) -> None:
    _write_stub(tmp_path, "docker", "echo '[]'\n")
    _assert_fail(_run("assert_docker_selinux_enabled", tmp_path), "docker_selinux")


def test_docker_selinux_enabled_violated_daemon_unreachable(tmp_path: Path) -> None:
    _write_stub(tmp_path, "docker", "exit 1\n")
    _assert_fail(_run("assert_docker_selinux_enabled", tmp_path), "docker_selinux")


# ---------------------------------------------------------------------------
# workspace_mount (/srv/werft: xfs, prjquota, nosuid, nodev)
# ---------------------------------------------------------------------------

_FINDMNT_OK_OPTS = "rw,relatime,attr2,inode64,logbufs=8,logbsize=32k,prjquota,nosuid,nodev"


def _findmnt_stub(fstype: str, opts: str) -> str:
    return (
        'case "$*" in\n'
        "  *FSTYPE*) echo '" + fstype + "' ;;\n"
        "  *OPTIONS*) echo '" + opts + "' ;;\n"
        "esac\n"
    )


def test_workspace_mount_satisfied(tmp_path: Path) -> None:
    _write_stub(tmp_path, "findmnt", _findmnt_stub("xfs", _FINDMNT_OK_OPTS))
    _assert_pass(_run("assert_workspace_mount", tmp_path))


def test_workspace_mount_violated_not_a_mount(tmp_path: Path) -> None:
    _write_stub(tmp_path, "findmnt", "exit 1\n")
    result = _run("assert_workspace_mount", tmp_path)
    _assert_fail(result, "workspace_mount")
    assert "not on a mount" in result.stderr


def test_workspace_mount_violated_wrong_fstype(tmp_path: Path) -> None:
    _write_stub(tmp_path, "findmnt", _findmnt_stub("ext4", _FINDMNT_OK_OPTS))
    result = _run("assert_workspace_mount", tmp_path)
    _assert_fail(result, "workspace_mount")
    assert "fstype ext4" in result.stderr


def test_workspace_mount_violated_missing_prjquota(tmp_path: Path) -> None:
    opts = "rw,relatime,attr2,inode64,logbufs=8,logbsize=32k,nosuid,nodev"
    _write_stub(tmp_path, "findmnt", _findmnt_stub("xfs", opts))
    result = _run("assert_workspace_mount", tmp_path)
    _assert_fail(result, "workspace_mount")
    assert "no pquota" in result.stderr


def test_workspace_mount_violated_missing_nosuid(tmp_path: Path) -> None:
    opts = "rw,relatime,attr2,inode64,logbufs=8,logbsize=32k,prjquota,nodev"
    _write_stub(tmp_path, "findmnt", _findmnt_stub("xfs", opts))
    result = _run("assert_workspace_mount", tmp_path)
    _assert_fail(result, "workspace_mount")
    assert "no nosuid" in result.stderr


def test_workspace_mount_violated_missing_nodev(tmp_path: Path) -> None:
    opts = "rw,relatime,attr2,inode64,logbufs=8,logbsize=32k,prjquota,nosuid"
    _write_stub(tmp_path, "findmnt", _findmnt_stub("xfs", opts))
    result = _run("assert_workspace_mount", tmp_path)
    _assert_fail(result, "workspace_mount")
    assert "no nodev" in result.stderr


# ---------------------------------------------------------------------------
# firewalld_posture (running, default zone drop, tailscale0 in trusted zone)
# ---------------------------------------------------------------------------


def _firewall_cmd_stub(state: str, zone: str, interfaces: str) -> str:
    return (
        'case "$*" in\n'
        "  *--state*) echo '" + state + "' ;;\n"
        "  *--get-default-zone*) echo '" + zone + "' ;;\n"
        "  *--list-interfaces*) echo '" + interfaces + "' ;;\n"
        "esac\n"
    )


def test_firewalld_posture_satisfied(tmp_path: Path) -> None:
    _write_stub(tmp_path, "firewall-cmd", _firewall_cmd_stub("running", "drop", "tailscale0 eth0"))
    _assert_pass(_run("assert_firewalld_posture", tmp_path))


def test_firewalld_posture_violated_not_running(tmp_path: Path) -> None:
    _write_stub(tmp_path, "firewall-cmd", _firewall_cmd_stub("not running", "drop", "tailscale0"))
    result = _run("assert_firewalld_posture", tmp_path)
    _assert_fail(result, "firewalld")
    assert "not running" in result.stderr


def test_firewalld_posture_violated_wrong_default_zone(tmp_path: Path) -> None:
    _write_stub(tmp_path, "firewall-cmd", _firewall_cmd_stub("running", "public", "tailscale0"))
    result = _run("assert_firewalld_posture", tmp_path)
    _assert_fail(result, "firewalld")
    assert "default zone public" in result.stderr


def test_firewalld_posture_violated_missing_tailscale0(tmp_path: Path) -> None:
    _write_stub(tmp_path, "firewall-cmd", _firewall_cmd_stub("running", "drop", "eth0"))
    result = _run("assert_firewalld_posture", tmp_path)
    _assert_fail(result, "firewalld")
    assert "tailscale0 not in trusted zone" in result.stderr


# ---------------------------------------------------------------------------
# tailscale_up
# ---------------------------------------------------------------------------


def test_tailscale_up_satisfied(tmp_path: Path) -> None:
    _write_stub(tmp_path, "tailscale", "echo '100.64.0.1'\n")
    _assert_pass(_run("assert_tailscale_up", tmp_path))


def test_tailscale_up_violated_absent(tmp_path: Path) -> None:
    _assert_fail(_run("assert_tailscale_up", tmp_path), "tailscale")


def test_tailscale_up_violated_not_up(tmp_path: Path) -> None:
    _write_stub(tmp_path, "tailscale", "exit 1\n")
    _assert_fail(_run("assert_tailscale_up", tmp_path), "tailscale")


# ---------------------------------------------------------------------------
# run_all_floors: accumulate-all semantics, not fail-fast
# ---------------------------------------------------------------------------


def _all_green_bin(bin_dir: Path) -> dict[str, str]:
    ldso = _write_stub(bin_dir, "ldso-stub", "echo 'x86-64-v3 (supported, required)'\n")
    _write_stub(bin_dir, "docker", "echo '29.6.2'\n")
    _write_stub(
        bin_dir, "containerd", "echo 'containerd github.com/containerd/containerd v2.2.6 abc'\n"
    )
    _write_stub(bin_dir, "uname", "echo '6.12.0-124.55.1.el10_1'\n")
    _write_stub(bin_dir, "getenforce", "echo 'Enforcing'\n")
    _write_stub(bin_dir, "findmnt", _findmnt_stub("xfs", _FINDMNT_OK_OPTS))
    _write_stub(bin_dir, "firewall-cmd", _firewall_cmd_stub("running", "drop", "tailscale0"))
    _write_stub(bin_dir, "tailscale", "echo '100.64.0.1'\n")
    return {"WERFT_LDSO": str(ldso)}


def test_run_all_floors_all_green(tmp_path: Path) -> None:
    """`docker` is invoked for both docker_version and docker_selinux, so
    the single stub must answer both shapes it's asked for."""
    env_extra = _all_green_bin(tmp_path)
    _write_stub(
        tmp_path,
        "docker",
        'case "$*" in\n'
        "  *SecurityOptions*) echo '[\"selinux\"]' ;;\n"
        "  *) echo '29.6.2' ;;\n"
        "esac\n",
    )
    result = _run("run_all_floors", tmp_path, env_extra)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_run_all_floors_multiple_violations_all_listed_not_first_fail(tmp_path: Path) -> None:
    """kernel and tailscale both fail; run_all_floors must accumulate both
    FLOOR FAIL lines rather than stopping at the first one."""
    env_extra = _all_green_bin(tmp_path)
    # Override the two floors we want to violate.
    _write_stub(tmp_path, "uname", "echo '6.12.0-124.0.0.el10_1'\n")  # kernel: below floor
    _write_stub(tmp_path, "tailscale", "exit 1\n")  # tailscale: not up
    _write_stub(
        tmp_path,
        "docker",
        'case "$*" in\n'
        "  *SecurityOptions*) echo '[\"selinux\"]' ;;\n"
        "  *) echo '29.6.2' ;;\n"
        "esac\n",
    )
    result = _run("run_all_floors", tmp_path, env_extra)
    assert result.returncode == 1
    assert "FLOOR FAIL kernel:" in result.stderr
    assert "FLOOR FAIL tailscale:" in result.stderr
