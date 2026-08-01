#!/bin/bash
# SPEC §4.2 / [CD§2.4(ii)] — the empirical CapAdd floor probe.
#
# SPEC §4.2 defers the exact CapAdd set to "one-afternoon empirical test during
# implementation (root + CapDrop=ALL, add caps only on observed failure) and then
# locked by the create-body test." This script is that test, committed as evidence
# and re-runnable by install.sh on the Rocky Linux 10 VM as an install-time assertion.
#
# Executed 2026-08-01 against Docker CE 29.6.2 / rockylinux/rockylinux:10.
# Adding one capability per observed failure produced this floor:
#
#   CHOWN DAC_OVERRIDE FOWNER KILL SETFCAP SETGID SETUID
#
# Discovered failures, in order:
#   CapDrop=ALL alone  -> dnf: "cpio: open/symlink failed - Permission denied"  => DAC_OVERRIDE
#   +DAC_OVERRIDE      -> chown EPERM; runuser "cannot set groups"             => CHOWN SETUID SETGID
#   +CHOWN/SETUID/SETGID -> chmod on a foreign-owned file EPERM                => FOWNER
#   +FOWNER            -> cross-uid kill EPERM                                 => KILL
#   +KILL              -> dnf httpd: "cpio: cap_set_file failed" (/usr/sbin/suexec) => SETFCAP
#
# NOT added, deliberately:
#   NET_BIND_SERVICE - unnecessary: Docker sets net.ipv4.ip_unprivileged_port_start=0
#                      in containers, so httpd binds :80 under this set (verified).
#   AUDIT_WRITE      - only sudo's audit message fails; cosmetic inside a root container.
#   SYS_ADMIN, SYS_PTRACE, SYS_MODULE, NET_ADMIN, NET_RAW - never, per SPEC §4.2.
#
# Run:
#   docker run --rm --cap-drop=ALL \
#     --cap-add=CHOWN --cap-add=DAC_OVERRIDE --cap-add=FOWNER --cap-add=KILL \
#     --cap-add=SETFCAP --cap-add=SETGID --cap-add=SETUID \
#     --security-opt no-new-privileges:true --pids-limit 4096 --shm-size 1g \
#     -v "$PWD/scripts/capfloor.sh:/capfloor.sh:ro" \
#     rockylinux/rockylinux:10 bash /capfloor.sh
#
# Every line must report EXIT=0. A non-zero line means the floor has moved and the
# create-body test (CAP_ADD_FLOOR) must be re-settled deliberately, never widened casually.
set +e
r() { printf '%-28s %s\n' "$1" "$2"; }

dnf install -y --setopt=install_weak_deps=False git nodejs npm httpd >/tmp/dnf.log 2>&1
r "dnf install (git/node/httpd)" "EXIT=$?"
r "  git" "$(git --version 2>&1 | head -1)"
r "  node" "$(node --version 2>&1 | head -1)"
r "  httpd" "$(httpd -v 2>&1 | head -1)"

npm config set prefix /usr/local >/dev/null 2>&1
npm install -g --silent semver@7.6.3 >/tmp/npm.log 2>&1
r "npm install -g" "EXIT=$?"

export UV_NO_MODIFY_PATH=1 HOME=/root
curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | sh >/tmp/uv.log 2>&1
r "uv install script" "EXIT=$? $(/root/.local/bin/uv --version 2>&1 | head -1)"

/root/.local/bin/uv venv /tmp/venv >/tmp/venv.log 2>&1
r "uv venv" "EXIT=$?"

useradd -m svcuser >/dev/null 2>&1
r "useradd" "EXIT=$?"
runuser -u svcuser -- id >/dev/null 2>&1
r "runuser (drop privs)" "EXIT=$?"

# Service start on a privileged port — no NET_BIND_SERVICE (see header).
mkdir -p /run/httpd /var/log/httpd /var/www/html
echo werft-capfloor > /var/www/html/index.html
httpd -k start >/tmp/httpd.log 2>&1
sleep 2
test "$(curl -sf http://localhost:80/ 2>/dev/null)" = "werft-capfloor"
r "service start + bind :80" "EXIT=$?"

chmod 777 /tmp
runuser -u svcuser -- sh -c 'echo $$ > /tmp/pid; exec sleep 30' &
sleep 1
kill -TERM "$(cat /tmp/pid)" >/dev/null 2>&1
r "cross-uid tree-kill" "EXIT=$?"

touch /tmp/owned
chown svcuser:svcuser /tmp/owned 2>/dev/null && chmod 600 /tmp/owned >/dev/null 2>&1
r "chown + chmod foreign file" "EXIT=$?"

r "CapEff" "$(grep CapEff /proc/self/status | tr -d '\t')"
r "unpriv_port_start" "$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null)"
