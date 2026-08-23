#!/bin/sh
# Generate dns-allow.conf: one server=/domain/<upstream> line per allowed host.
# Usage: gen-dns-allow.sh <dispatch-config.json> <out-file> [upstream]
# The deployed <out-file> is /opt/werft/config/dnsmasq.d/dns-allow.conf — the
# directory (not the file) is what compose bind-mounts into dns-guard at
# /etc/dnsmasq.d, because the atomic `mv` below replaces the file's inode and a
# single-FILE bind mount would pin the old one. After regenerating, RESTART
# dns-guard (`docker compose ... restart dns-guard`): dnsmasq does NOT re-read
# its config on SIGHUP (HUP only re-reads /etc/hosts and clears the cache).
# Union across projects (per-slot DNS views are post-milestone — T9 scope note).
set -eu
CONF="$1"; OUT="$2"; UP="${3:-1.1.1.1}"
python3 - "$CONF" "$UP" <<'EOF' > "$OUT.tmp"
import json, sys
PRESETS = {
    "npm": ["registry.npmjs.org"],
    "pypi": ["pypi.org", "files.pythonhosted.org"],
    "dnf-rocky": ["mirrors.rockylinux.org", "dl.rockylinux.org"],
    "crates": ["crates.io", "static.crates.io", "index.crates.io"],
    "go": ["proxy.golang.org", "sum.golang.org"],
}
conf, up = sys.argv[1], sys.argv[2]
hosts = {"github.com", "api.github.com", "codeload.github.com",
         "objects.githubusercontent.com", "api.anthropic.com"}
try:
    projects = json.load(open(conf)).get("projects", {})
except FileNotFoundError:
    projects = {}
for p in projects.values():
    for preset in p.get("registries", []):
        hosts.update(PRESETS.get(preset, []))
    hosts.update(h.lower() for h in p.get("extra_hosts", []))
for h in sorted(hosts):
    print(f"server=/{h}/{up}")
EOF
mv "$OUT.tmp" "$OUT"
