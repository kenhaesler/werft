#!/usr/bin/env bash
# Werft host installer (SPEC §2/§10, BUILD-PLAN P0.2). Idempotent driver:
#   --check           run the floor sweep only (no changes), exit with its status.
#   --ntfy-host HOST  append ".HOST" to the manager egress allowlist.
#
# Full run order: pre-install floors that don't depend on install steps
# (x86-64-v3, selinux enforcing, tailscale up, workspace mount) -> install
# Docker CE (repo + docker-ce + compose plugin + container-selinux) -> write
# /etc/docker/daemon.json {"selinux-enabled": true} (merge-preserving) and
# restart docker -> create werft user + directories (+ SELinux fcontext and
# restorecon on the bind-mounted trees) -> seed the egress allowlist -> ensure
# /opt/werft/config/dnsmasq.d/dns-allow.conf exists -> firewalld
# posture (default zone drop, tailscale0 trusted) -> install the backup /
# restore-drill systemd units (deploy/backup/, Task 14) -> re-run the full
# floor sweep and exit with its status (the installer's last word).
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
# SCRIPTDIR so `shellcheck -x` resolves the source relative to this file rather
# than to whatever directory the check happens to run from.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=./floors.sh
. "${SCRIPT_DIR}/floors.sh"

# Squid container uid:gid — resolved empirically (deploy/egress-proxy image
# runs squid as the distro squid user). Verified 2026-08-23 by building
# deploy/egress-proxy and running `id -u squid; id -g squid` inside it
# against the rockylinux/rockylinux base pinned there:
#   docker build -q --platform linux/amd64 deploy/egress-proxy
#   docker run --rm --entrypoint /bin/sh <image> -c 'id -u squid; id -g squid'
#   => 23
SQUID_UID=23
SQUID_GID=23
# dnsmasq log-writer uid inside deploy/dns-guard's image (verified Task 10).
DNSGUARD_UID=999
DNSGUARD_GID=0

NTFY_HOST=""
CHECK_ONLY=0

usage() {
    echo "Usage: $0 [--check] [--ntfy-host HOST]" >&2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1; shift ;;
        --ntfy-host) NTFY_HOST="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

if [ "$CHECK_ONLY" -eq 1 ]; then
    run_all_floors
    exit $?
fi

need_root() {
    [ "$(id -u)" -eq 0 ] || { echo "install.sh: full install must run as root" >&2; exit 1; }
}

pre_install_floors() {
    local rc=0 f
    for f in assert_x86_64_v3 assert_selinux_enforcing assert_tailscale_up assert_workspace_mount; do
        "$f" || rc=1
    done
    return $rc
}

install_docker() {
    command -v dnf >/dev/null 2>&1 || { echo "install.sh: dnf not found (Rocky Linux 10 required)" >&2; exit 1; }
    dnf -y install dnf-plugins-core
    dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
    # python3 is required later by write_daemon_json's JSON merge and by
    # ensure_dns_allow_conf's call into gen-dns-allow.sh — a minimal Rocky 10
    # install does not guarantee it, so pull it in alongside Docker CE here.
    # policycoreutils-python-utils provides `semanage`, which apply_selinux_labels
    # needs to set container_file_t on the bind-mounted trees.
    dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin \
        container-selinux python3 policycoreutils-python-utils
    systemctl enable --now docker
}

write_daemon_json() {
    command -v python3 >/dev/null 2>&1 \
        || { echo "install.sh: python3 not found — cannot merge /etc/docker/daemon.json" >&2; exit 1; }
    local daemon_json="/etc/docker/daemon.json"
    mkdir -p /etc/docker
    python3 - "$daemon_json" <<'PYEOF'
import json
import sys

path = sys.argv[1]
try:
    with open(path) as fh:
        data = json.load(fh)
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
data["selinux-enabled"] = True
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PYEOF
    systemctl restart docker
}

create_layout() {
    if ! id werft >/dev/null 2>&1; then
        useradd --system --create-home --home-dir /opt/werft --shell /sbin/nologin werft
    fi

    install -d -m 0755 /opt/werft/compose /opt/werft/config /opt/werft/backups
    # dnsmasq.d is bind-mounted into dns-guard as a DIRECTORY (compose.yaml):
    # gen-dns-allow.sh replaces dns-allow.conf via `mv` (new inode), which a
    # single-file bind mount would never see.
    install -d -m 0755 /opt/werft/config/dnsmasq.d
    install -d -m 0700 /opt/werft/secrets
    install -d -m 0755 /srv/werft/runs
    install -d -m 0755 /srv/werft/egress/allow
    install -d -m 0755 /srv/werft/egress/log/squid
    install -d -m 0755 /srv/werft/egress/log/dnsguard

    chown -R werft:werft /opt/werft /srv/werft
    # Container-writer overrides: squid and dnsmasq write into these bind
    # mounts as their in-image uid, not as the werft host user.
    chown "${SQUID_UID}:${SQUID_GID}" /srv/werft/egress/log/squid
    chown "${DNSGUARD_UID}:${DNSGUARD_GID}" /srv/werft/egress/log/dnsguard

    apply_selinux_labels
}

# SELinux file labels for the bind-mounted trees. The floors mandate Enforcing
# plus dockerd `selinux-enabled: true`, which means an unlabelled host directory
# is simply invisible (EACCES) to a confined container — so labelling is part of
# creating the layout, not an afterthought.
#
# `semanage fcontext` writes the *persistent* default type (survives relabels
# and new files); `restorecon` applies it to what already exists. Compose's `:z`
# on the small shared mounts covers the runtime shared-label bit; this covers
# /srv/werft, which is deliberately NOT `:z`-mounted (a recursive relabel at
# every manager start would stomp the per-run workspace `:Z` labels).
apply_selinux_labels() {
    if ! command -v semanage >/dev/null 2>&1; then
        echo "WARN: semanage not found (policycoreutils-python-utils) — SELinux" \
             "file contexts NOT set for /srv/werft and /opt/werft/config." \
             "Containers WILL fail with permission-denied on their bind mounts" \
             "under Enforcing. Install policycoreutils-python-utils and re-run." >&2
        return 0
    fi
    local tree
    for tree in "/srv/werft(/.*)?" "/opt/werft/config(/.*)?"; do
        # -a fails when the rule already exists (idempotency): fall back to -m.
        semanage fcontext -a -t container_file_t "$tree" 2>/dev/null \
            || semanage fcontext -m -t container_file_t "$tree" \
            || { echo "WARN: semanage fcontext failed for $tree" >&2; continue; }
    done
    if command -v restorecon >/dev/null 2>&1; then
        restorecon -R /srv/werft /opt/werft/config \
            || echo "WARN: restorecon failed for /srv/werft /opt/werft/config" >&2
    else
        echo "WARN: restorecon not found — labels written but not applied" >&2
    fi
}

seed_egress_allow() {
    local allow_dir="/srv/werft/egress/allow"
    install -d -m 0755 "$allow_dir"

    {
        echo ".github.com"
        echo ".api.github.com"
        echo ".codeload.github.com"
        echo ".objects.githubusercontent.com"
        [ -n "$NTFY_HOST" ] && echo ".${NTFY_HOST}"
    } > "${allow_dir}/manager.txt"
    chmod 0644 "${allow_dir}/manager.txt"

    # slotN.txt are only-if-absent: they're rewritten live by the manager
    # (per-run egress allowlists) and HUP'd into squid. A maintenance re-run
    # of install.sh must never truncate a currently-running slot's allowlist
    # out from under a live run — manager.txt above stays rewrite-on-run
    # because it's operator-owned config (e.g. --ntfy-host can change).
    local i
    for i in 0 1 2 3; do
        [ -f "${allow_dir}/slot${i}.txt" ] || : > "${allow_dir}/slot${i}.txt"
        chmod 0644 "${allow_dir}/slot${i}.txt"
    done
}

ensure_dns_allow_conf() {
    command -v python3 >/dev/null 2>&1 \
        || { echo "install.sh: python3 not found — cannot generate dns-allow.conf" >&2; exit 1; }
    local gen="${SCRIPT_DIR}/../dns-guard/gen-dns-allow.sh"
    # Inside the DIRECTORY compose bind-mounts at /etc/dnsmasq.d (see
    # create_layout) — never a bare /opt/werft/config/dns-allow.conf, which a
    # single-file mount would pin to a stale inode across regeneration.
    local out="/opt/werft/config/dnsmasq.d/dns-allow.conf"
    local dispatch="/opt/werft/config/dispatch.json"
    # gen-dns-allow.sh tolerates a missing config path and emits the
    # base-only allowlist — used here to guarantee $out exists even before
    # a dispatch config is deployed, so compose never mount-creates a
    # directory in its place at first `up`.
    if [ -f "$dispatch" ]; then
        "$gen" "$dispatch" "$out"
    else
        "$gen" "${dispatch}.missing" "$out"
    fi
}

apply_firewalld_posture() {
    firewall-cmd --set-default-zone=drop
    firewall-cmd --permanent --zone=trusted --add-interface=tailscale0
    firewall-cmd --reload
}

install_backup_units() {
    local unit_dir="${SCRIPT_DIR}/../backup"
    local unit
    for unit in werft-backup.service werft-backup.timer \
                werft-restore-drill.service werft-restore-drill.timer; do
        if [ -f "${unit_dir}/${unit}" ]; then
            install -m 0644 "${unit_dir}/${unit}" "/etc/systemd/system/${unit}"
        else
            echo "WARN: ${unit_dir}/${unit} not found — skipping (Task 14 units not present)" >&2
        fi
    done

    systemctl daemon-reload
    local timer
    for timer in werft-backup.timer werft-restore-drill.timer; do
        if [ -f "/etc/systemd/system/${timer}" ]; then
            systemctl enable --now "$timer"
        fi
    done
}

need_root

pre_install_floors || { echo "install.sh: pre-install floors failed, aborting" >&2; exit 1; }
install_docker
write_daemon_json
create_layout
seed_egress_allow
ensure_dns_allow_conf
apply_firewalld_posture
install_backup_units

# Last word: the full floor sweep, exit status = the sweep's.
run_all_floors
exit $?
