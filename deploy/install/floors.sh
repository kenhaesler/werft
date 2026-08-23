#!/usr/bin/env bash
# Werft install floors (SPEC §2, BUILD-PLAN P0.2). Each assert_* is standalone
# and stub-testable: it consults only $PATH commands and readable files.
#
# Floor sources (BUILD-PLAN §15.1: floors are policy, values re-checked dated):
#   x86_64_v3          — SPEC §2 (default qemu64 CPU model fails this)
#   docker_version      — C1 (29.6.2 floor; 29.4.2 is the known trap release, refused outright)
#   containerd_version   — D13: containerd bundled with Docker CE 29.6.2. Verified 2026-08-23
#                          via docs.docker.com/engine/release-notes/29/ and
#                          github.com/moby/moby/releases/tag/docker-v29.6.2 ("Packaging
#                          updates: Update containerd (static binaries) to v2.2.6").
#   kernel              — D13: Rocky Linux 10 kernel that fixed CVE-2026-31431 ("CopyFail").
#                          Verified 2026-08-23 via RHSA-2026:13566 (Red Hat Enterprise Linux
#                          10 kernel errata, https://access.redhat.com/errata/RHSA-2026:13566)
#                          and rockylinux.org/news/2026-05-11-copyfail-cve-2026-31431, both
#                          naming kernel-6.12.0-124.55.1.el10_1 as the first fixed build.
#   selinux/docker_selinux/workspace_mount/firewalld/tailscale — SPEC §2, §10.
set -u

# Dated floor pins (BUILD-PLAN §15.1: floors are policy, values re-checked dated).
DOCKER_FLOOR="29.6.2"          # C1; and 29.4.2 is refused outright below
DOCKER_REFUSED="29.4.2"
CONTAINERD_FLOOR="2.2.6"       # D13: containerd bundled with Docker CE 29.6.2 (see header)
KERNEL_FLOOR="6.12.0-124.55.1.el10_1"  # D13: RHSA-2026:13566 fix for CVE-2026-31431 (see header)

_fail() { echo "FLOOR FAIL $1: $2" >&2; return 1; }
_vergte() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

assert_x86_64_v3() {
    local out ldso
    ldso="${WERFT_LDSO:-/lib64/ld-linux-x86-64.so.2}"
    out=$("$ldso" --help 2>/dev/null | grep -F 'x86-64-v3')
    case "$out" in *"(supported"*) return 0 ;; esac
    _fail x86_64_v3 "CPU does not report x86-64-v3 (SPEC §2; default qemu64 fails this)"
}

assert_docker_version() {
    local v
    v=$(docker version --format '{{.Server.Version}}' 2>/dev/null) \
        || return $(_fail docker_version "docker daemon unreachable")
    [ "$v" = "$DOCKER_REFUSED" ] && return $(_fail docker_version "$v is refused (C1 trap release)")
    _vergte "$v" "$DOCKER_FLOOR" || _fail docker_version "$v < floor $DOCKER_FLOOR"
}

assert_containerd_version() {
    local v
    v=$(containerd --version 2>/dev/null | awk '{print $3}' | tr -d v) \
        || return $(_fail containerd "containerd not found")
    _vergte "$v" "$CONTAINERD_FLOOR" || _fail containerd "$v < floor $CONTAINERD_FLOOR"
}

assert_kernel_floor() {
    local v; v=$(uname -r)
    _vergte "$v" "$KERNEL_FLOOR" || _fail kernel "$v < floor $KERNEL_FLOOR (CVE-2026-31431/KEV)"
}

assert_selinux_enforcing() {
    [ "$(getenforce 2>/dev/null)" = "Enforcing" ] || _fail selinux "getenforce != Enforcing"
}

assert_docker_selinux_enabled() {
    docker info --format '{{json .SecurityOptions}}' 2>/dev/null | grep -q selinux \
        || _fail docker_selinux "docker info lacks selinux SecurityOption (daemon.json selinux-enabled)"
}

assert_workspace_mount() {
    local opts fstype
    fstype=$(findmnt -no FSTYPE --target /srv/werft 2>/dev/null) \
        || return $(_fail workspace_mount "/srv/werft not on a mount")
    [ "$fstype" = "xfs" ] || return $(_fail workspace_mount "fstype $fstype != xfs")
    opts=$(findmnt -no OPTIONS --target /srv/werft)
    case "$opts" in *prjquota*|*pquota*) : ;; *) return $(_fail workspace_mount "no pquota in [$opts]") ;; esac
    case "$opts" in *nosuid*) : ;; *) return $(_fail workspace_mount "no nosuid") ;; esac
    case "$opts" in *nodev*)  : ;; *) return $(_fail workspace_mount "no nodev")  ;; esac
}

assert_firewalld_posture() {
    [ "$(firewall-cmd --state 2>/dev/null)" = "running" ] \
        || return $(_fail firewalld "firewalld not running")
    local dz; dz=$(firewall-cmd --get-default-zone)
    [ "$dz" = "drop" ] || return $(_fail firewalld "default zone $dz != drop")
    firewall-cmd --zone=trusted --list-interfaces | grep -qw tailscale0 \
        || _fail firewalld "tailscale0 not in trusted zone"
}

assert_tailscale_up() {
    tailscale ip -4 >/dev/null 2>&1 || _fail tailscale "tailscale not up"
}

run_all_floors() {
    local rc=0 f
    for f in assert_x86_64_v3 assert_docker_version assert_containerd_version \
             assert_kernel_floor assert_selinux_enforcing assert_docker_selinux_enabled \
             assert_workspace_mount assert_firewalld_posture assert_tailscale_up; do
        "$f" || rc=1
    done
    return $rc
}
