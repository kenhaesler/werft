# Werft deploy RUNBOOK

This is the operator-executed acceptance for the T9 deploy milestone
(issue #26): a fresh Rocky 10 VM to a healthy, egress-gated, backed-up
Werft manager. Every stage names the exact command and the expected output
shape. Read `deploy/README.md` first for what each `.env`/`WERFT_*` key
means — this document is the sequence, not the reference.

Six stages, run in order:

1. Fresh-VM prerequisites
2. `install.sh` — floor sweep green, then one floor demonstrated failing loudly
3. Secrets, digests, `docker compose up`
4. Manager healthy on the tailnet, unreachable elsewhere
5. Live egress acceptance (the T7/T8 seam, activated)
6. Backup + restore drill, run once by hand

---

## Stage 1 — Fresh Rocky 10 VM prerequisites

The VM must already have Tailscale installed and this repository cloned
(or `rsync`'d) before `install.sh` runs — `install.sh` does not install
Tailscale itself (`assert_tailscale_up` only checks it, per
`deploy/install/floors.sh`).

```sh
sudo tailscale up
tailscale ip -4          # note this address — it becomes TS_IP later
```

Expected: a `100.x.y.z` address prints; `tailscale status` shows the node
online.

`/srv/werft` must be its own XFS filesystem mounted with project quota
enabled before `install.sh` runs (`assert_workspace_mount` in
`floors.sh` requires fstype `xfs`, a `pquota`/`prjquota` mount option,
`nosuid`, and `nodev`). Provision the block device, then add the exact
fstab line (adjust the device path to your VM):

```
/dev/vdb  /srv/werft  xfs  defaults,pquota,nosuid,nodev  0  2
```

```sh
sudo mkfs.xfs /dev/vdb
sudo mkdir -p /srv/werft
echo '/dev/vdb  /srv/werft  xfs  defaults,pquota,nosuid,nodev  0  2' | sudo tee -a /etc/fstab
sudo mount -a
findmnt -no FSTYPE,OPTIONS --target /srv/werft
```

Expected: `xfs` and an options list containing `pquota` (or `prjquota`),
`nosuid`, `nodev`.

---

## Stage 2 — Clone, install, and prove a floor fails loudly

```sh
sudo mkdir -p /opt/werft
sudo git clone https://github.com/<org>/werft.git /opt/werft/src
```

`install.sh` and the systemd backup units it installs both assume the
**whole `deploy/` tree** is staged at `/opt/werft/compose` (this is the
directory `werft-backup.sh`, `werft-restore-drill.sh`, and the compose
invocations in this RUNBOOK all reference as `/opt/werft/compose/...`).
Cloning the repo does not do this by itself — stage it explicitly:

```sh
sudo mkdir -p /opt/werft/compose
sudo rsync -a --delete /opt/werft/src/deploy/ /opt/werft/compose/
```

(Re-run this `rsync` on every deploy of a new commit — `install.sh` does
not do it for you; it only reads `deploy/install/`, `deploy/dns-guard/`,
and `deploy/backup/` relative to *itself*, i.e. wherever it's invoked
from.)

Run the floor sweep only, before touching anything:

```sh
sudo /opt/werft/src/deploy/install/install.sh --check
```

Expected: exits 0 with no `FLOOR FAIL` lines (a fresh VM without Docker
installed yet will legitimately fail `assert_docker_version` — that's
expected pre-install; the post-install sweep below is the one that must
be clean).

Run the full install:

```sh
sudo /opt/werft/src/deploy/install/install.sh --ntfy-host ntfy.example.com
```

Expected: Docker CE installs, `/etc/docker/daemon.json` gains
`"selinux-enabled": true`, `/opt/werft/{compose,config,secrets,backups}`
and `/srv/werft/{runs,egress/{allow,log/{squid,dnsguard}}}` are created,
`/srv/werft/egress/allow/manager.txt` is seeded with the four GitHub
hosts plus `.ntfy.example.com`, `/opt/werft/config/dns-allow.conf` exists,
firewalld's default zone is `drop` with `tailscale0` in the `trusted`
zone, the backup/restore-drill systemd timers are enabled, and the
script's last action — `run_all_floors` — exits 0 with no `FLOOR FAIL`
lines. A non-zero exit here means a floor is still unmet; do not proceed.

**Demonstrate a floor failing loudly** (the floor-violation drill), then
restore it:

```sh
sudo firewall-cmd --set-default-zone=public
sudo /opt/werft/src/deploy/install/install.sh --check; echo "exit=$?"
```

Expected: stderr contains `FLOOR FAIL firewalld: default zone public !=
drop`, and `exit=1`. Restore immediately:

```sh
sudo firewall-cmd --set-default-zone=drop
sudo /opt/werft/src/deploy/install/install.sh --check; echo "exit=$?"
```

Expected: no `FLOOR FAIL` lines, `exit=0`.

---

## Stage 3 — Digests, secrets, first `up`

Resolve the two pulled image digests and write `.env`:

```sh
cd /opt/werft/compose
cp .env.example .env
docker buildx imagetools inspect postgres:18-alpine        # copy the Digest: line
docker buildx imagetools inspect tecnativa/docker-socket-proxy:latest
$EDITOR .env   # fill TS_IP, POSTGRES_IMAGE=postgres:18-alpine@sha256:..., SOCKET_PROXY_IMAGE=...@sha256:..., GITHUB_APP_CLIENT_ID, NTFY_URL
```

Seed each secret file under `/opt/werft/secrets/` (created `0700` by
`install.sh`), naming the command that produces it:

```sh
# pg_password — compose's postgres and manager-migrate/manager both read this
openssl rand -hex 32 | sudo tee /opt/werft/secrets/pg_password >/dev/null

# api_token — the static bearer token guarding /api/v1 (SPEC §9)
openssl rand -hex 32 | sudo tee /opt/werft/secrets/api_token >/dev/null

# github_app_key — the GitHub App's RS256 private key PEM, downloaded from
# the App's settings page (Private keys -> Generate a private key)
sudo cp ~/Downloads/werft-app.*.private-key.pem /opt/werft/secrets/github_app_key

# claude_credential — the provider credential the manager builds runner env from
claude setup-token | sudo tee /opt/werft/secrets/claude_credential >/dev/null

# ntfy_token — optional; leave empty/absent if the ntfy topic needs no auth
openssl rand -hex 24 | sudo tee /opt/werft/secrets/ntfy_token >/dev/null

sudo chmod 0600 /opt/werft/secrets/*
sudo chown werft:werft /opt/werft/secrets/*
```

Deploy the dispatch config and generate the DNS allowlist from it:

```sh
sudo $EDITOR /opt/werft/config/dispatch.json   # per-project registries/extra_hosts (SPEC §4.5)
sudo /opt/werft/compose/dns-guard/gen-dns-allow.sh \
    /opt/werft/config/dispatch.json /opt/werft/config/dns-allow.conf
```

Expected: `dns-allow.conf` now has one `server=/<host>/1.1.1.1` line per
allowed host (base GitHub hosts + `api.anthropic.com` + every configured
project's expanded registries/extra_hosts).

Validate, then bring the stack up:

```sh
cd /opt/werft/compose
docker compose config -q
docker compose up -d --build
docker compose ps
```

Expected: `config -q` prints nothing (exit 0). `ps` shows `postgres`
healthy, `manager-migrate` exited (0), and `manager`, `docker-socket-proxy`,
`egress-proxy`, `dns-guard` all `running`.

---

## Stage 4 — Manager healthy, reachable only from the tailnet

```sh
curl -fsS http://$(tailscale ip -4):8420/healthz
```

Expected: `{"status":"ok"}` (or equivalent 200 JSON body) — this route is
unauthenticated by design and is the liveness check.

```sh
curl -fsS -H "Authorization: Bearer $(cat /opt/werft/secrets/api_token)" \
    http://$(tailscale ip -4):8420/api/v1/runs
```

Expected: `200` with a JSON `{"runs": [...]}` body (empty list before any
project is onboarded).

From a host **not** on the tailnet (or with `tailscale down` locally),
confirm there is no public listener:

```sh
curl -m 5 http://<VM's public/LAN IP>:8420/healthz; echo "exit=$?"
```

Expected: connection refused/timeout, non-zero exit — `ports:` in
`compose.yaml` binds only `${TS_IP}:8420:8420`, and firewalld's default
`drop` zone blocks anything not in the `trusted` (tailscale0) zone anyway.

---

## Stage 5 — Live egress acceptance (T7 decision 19 / T8 D7, activated)

This is the first **live**, full-agent acceptance of the egress evidence
seam. T7's acceptance used a busybox stand-in for the runner; T8's egress
test exercised `extract_egress_lines` against a seeded, synthetic
squid/dnsmasq log — this stage is the first run where a real agent
container, behind the real squid proxy, produces the evidence itself.

Onboard a project:

```sh
curl -fsS -X POST \
    -H "Authorization: Bearer $(cat /opt/werft/secrets/api_token)" \
    -H "Content-Type: application/json" \
    -d '{"slug":"accept-t9","owner":"<gh-owner>","repo":"<gh-repo>"}' \
    http://$(tailscale ip -4):8420/api/v1/projects/onboard
```

Expected: `201` with a `ProjectOut` JSON body (`id`, `slug`, `state`, …).
Requires `GITHUB_APP_CLIENT_ID`/the App key to already be configured
(Stage 3) — a 503 here means GitHub isn't wired up yet.

Dispatch a real run by leaving an eligible issue open in the onboarded
repo's backlog (the manager polls issues every `issue_poll_seconds`,
default 60s, SPEC §6.2) — any open, unlabeled-ineligible issue is
dispatchable; do not close/unlabel it before the run claims it. Watch it
progress:

```sh
curl -fsS -H "Authorization: Bearer $(cat /opt/werft/secrets/api_token)" \
    http://$(tailscale ip -4):8420/api/v1/runs | jq '.runs[] | {id, state}'
```

Expected: a run transitions `queued` -> `claimed` -> `running` ->
(terminal state). Once terminal, list its collected artifacts:

```sh
RUN_ID=<the run's id from the listing above>
curl -fsS -H "Authorization: Bearer $(cat /opt/werft/secrets/api_token)" \
    "http://$(tailscale ip -4):8420/api/v1/runs/${RUN_ID}/artifacts" | jq .
```

Expected: the `artifacts` array includes rows with
`"path": "egress/squid-access.log"` and `"path": "egress/dns-guard.log"`
(the collector's staged filenames — see
`manager/werft/orchestrator/evidence.py`'s `_EGRESS_LOGS`; note this is
`dns-guard.log`, not `dns-guard-queries.log`), each with `bytes > 0`.

Fetch the two files and grep them directly:

```sh
curl -fsS -H "Authorization: Bearer $(cat /opt/werft/secrets/api_token)" \
    "http://$(tailscale ip -4):8420/api/v1/runs/${RUN_ID}/artifacts/egress/squid-access.log" \
    -o squid-access.log
curl -fsS -H "Authorization: Bearer $(cat /opt/werft/secrets/api_token)" \
    "http://$(tailscale ip -4):8420/api/v1/runs/${RUN_ID}/artifacts/egress/dns-guard.log" \
    -o dns-guard.log

grep -E 'CONNECT|GET' squid-access.log      # the agent's real fetch(es)
grep -E 'query\[' dns-guard.log             # the agent's real DNS lookup(s)
```

Expected: `squid-access.log` contains at least one line for a host the
run's `egress_hosts()` allowlist covers (e.g. `github.com` or
`api.anthropic.com`), and `dns-guard.log` contains at least one `query[A]`
line for the same host, both timestamped inside the run's `claimed`..
terminal window — proof the run reached the network only through squid/
dns-guard, and that the evidence pipeline captured it live.

---

## Stage 6 — Backup and restore drill, run once by hand

`werft-backup.sh` needs `restic_repo`/`restic_password` seeded first
(these are **not** part of `compose.yaml`'s secrets — they're host-side
files the backup script reads directly):

```sh
echo "<your restic repository URL>" | sudo tee /opt/werft/secrets/restic_repo >/dev/null
openssl rand -base64 32 | sudo tee /opt/werft/secrets/restic_password >/dev/null
sudo chmod 0600 /opt/werft/secrets/restic_repo /opt/werft/secrets/restic_password
restic -r "$(cat /opt/werft/secrets/restic_repo)" init   # first time only
```

Trigger the backup once by hand:

```sh
sudo systemctl start werft-backup.service
sudo systemctl status werft-backup.service --no-pager
restic -r "$(cat /opt/werft/secrets/restic_repo)" snapshots
```

Expected: `status` shows `Active: inactive (dead)` with the last run
exiting `0`; `snapshots` lists a new snapshot covering
`/opt/werft/backups/dumps`, `/opt/werft/compose`, `/opt/werft/config`,
`/srv/werft/runs` (the GitHub App key is deliberately excluded — D9).

Run the restore drill once by hand:

```sh
sudo systemctl start werft-restore-drill.service
sudo systemctl status werft-restore-drill.service --no-pager
```

Expected: the unit exits 0; if `ntfy_url` is configured, a
`"restore drill OK, N runs"` notification arrives. A failure notifies
`"werft-restore-drill FAILED on <host>: line <N>"` — a backup that has
never been restored is not a backup, so a failure here blocks acceptance
until fixed.

---

## Known properties and trade-offs

Operator-actionable facts about this deploy that are true by design, not
bugs to file:

1. Flipping `WERFT_EGRESS_SLOT_COUNT` to `0` (or changing
   `WERFT_EGRESS_SUBNET_PREFIX`) while slot networks are live strands
   squid/dns-guard's attachments to those networks — the change takes
   full effect only after every run using the old slot layout has drained
   naturally; to force it immediately, `docker network disconnect -f
   werft-net-<run_id> werft-egress-proxy` (and the same for
   `werft-dns-guard`) by hand for each live run network.
2. The nightly backup (`werft-backup.sh`, 03:30 by default) rotates
   squid's access log (`docker kill -s USR1 werft-egress-proxy`) *after*
   the restic push, so a run whose egress activity spans the 03:30 window
   may find its pre-rotation squid lines already rotated away by the time
   Stage 5's evidence extraction runs against the live log — the lines
   are still in the snapshot taken just before rotation, just not in the
   live file.
3. `install.sh` and the backup/restore-drill systemd units both assume
   the whole `deploy/` tree is staged at `/opt/werft/compose` (Stage 2's
   `rsync`) — a bare `git clone` left at some other path breaks the unit
   files' `ExecStart` paths and `install.sh`'s own relative lookups of
   `../dns-guard/gen-dns-allow.sh` and `../backup/*`.
4. The `manager` container reaches the internet directly through the
   `ingress`/NAT-capable networks it's attached to — its own
   `HTTP_PROXY`/`HTTPS_PROXY` env (pointed at `egress-proxy`) is honored
   only by libraries that respect those variables. squid's `manager.txt`
   allowlist is **not** a hard boundary for the manager process itself
   (a tool that ignores proxy env still has a route out); it **is** a
   hard boundary for runner containers, whose per-run networks are
   `Internal: true` — a tool that ignores proxy env inside a runner has
   no route at all.
5. `/srv/werft/egress/log/squid` must be writable by uid 23 (the squid
   image's in-container `squid` user) and `/srv/werft/egress/log/dnsguard`
   by uid 999 (`install.sh`'s `create_layout` does this via `chown`). If
   `access.log`/`queries.log` stay empty after `up`, check ownership on
   those two directories first before suspecting a config bug.
6. A missing `/opt/werft/config/dns-allow.conf` at the very first
   `docker compose up` makes Docker create an empty **directory** at that
   bind-mount path instead of mounting a file, and `dns-guard` then fails
   to start (`dnsmasq` can't load a directory as its config).
   `install.sh`'s `ensure_dns_allow_conf` creates the file first, so a
   normal Stage 2 install never hits this — but if the file is ever
   deleted afterward, re-create it with
   `deploy/dns-guard/gen-dns-allow.sh /opt/werft/config/dispatch.json /opt/werft/config/dns-allow.conf`
   before the next `up`, not by hand-creating an empty file (which would
   also work but loses the generated allowlist content).
