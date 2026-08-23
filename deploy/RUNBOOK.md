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
hosts plus `.ntfy.example.com`,
`/opt/werft/config/dnsmasq.d/dns-allow.conf` exists,
firewalld's default zone is `drop` with `tailscale0` in the `trusted`
zone, the backup/restore-drill systemd timers are enabled, and the
script's last action — `run_all_floors` — exits 0 with no `FLOOR FAIL`
lines. A non-zero exit here means a floor is still unmet; do not proceed.

**SELinux labelling happens here**, inside `create_layout`: `install.sh`
sets the persistent default type on the two bind-mounted trees
(`semanage fcontext -a -t container_file_t "/srv/werft(/.*)?"` and the same
for `/opt/werft/config(/.*)?`) and then `restorecon -R`s them. Confirm it
landed rather than warning:

```sh
sudo semanage fcontext -l | grep -E '/srv/werft|/opt/werft/config'
ls -Zd /srv/werft /srv/werft/runs /opt/werft/config/dnsmasq.d
```

Expected: two `container_file_t` rules listed, and `ls -Z` shows
`container_file_t` on each path. A `WARN: semanage not found` line in the
install output means `policycoreutils-python-utils` is missing — install it
and re-run `install.sh`; under Enforcing, unlabelled bind mounts are
`permission denied` to every container in the stack.

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
    /opt/werft/config/dispatch.json \
    /opt/werft/config/dnsmasq.d/dns-allow.conf
```

Expected: `dns-allow.conf` now has one `server=/<host>/1.1.1.1` line per
allowed host (base GitHub hosts + `api.anthropic.com` + every configured
project's expanded registries/extra_hosts). Note the output path: it is
inside `dnsmasq.d/`, the **directory** compose bind-mounts at
`/etc/dnsmasq.d` — never a bare `/opt/werft/config/dns-allow.conf` (see
`deploy/README.md`, "Rolling out an egress allowlist change", for why the
mount is a directory and why a regeneration needs a `restart`, not a HUP).

**Set the dispatch quota ceiling.** `WERFT_QUOTA_CEILING_SECONDS` defaults
to `0`, and at `0` the manager refuses to dispatch anything — Stage 5 below
cannot produce a run until it is set. `compose.yaml` deliberately does not
pick a value (it is the operator's SPEC §7 knob, not a deploy-stack fact —
see `deploy/README.md`'s "Not set in `compose.yaml`" paragraph). Add it to
the `manager` service's `environment:` block before the first `up`:

```sh
sudo $EDITOR /opt/werft/compose/compose.yaml
#   under services.manager.environment, alongside the other WERFT_* keys:
#     WERFT_QUOTA_CEILING_SECONDS: "18000"    # 5h of agent wall-clock per window
```

Expected: `docker compose config` shows the key on the `manager` service.
Choose the number deliberately — it is the ceiling the orchestrator refuses
to dispatch past, not a hint.

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

**Preconditions** (all from Stage 3 — check them before starting, a miss
here shows up as "nothing ever dispatches"):

- `WERFT_QUOTA_CEILING_SECONDS` is set to a non-zero value on the `manager`
  service. At the `0` default the dispatch plane never claims a run and the
  manager logs `app.dispatch_disabled_no_quota_ceiling` once at boot —
  `docker compose logs manager | grep dispatch_disabled` is the fastest way
  to catch it.
- `GITHUB_APP_CLIENT_ID` and `/opt/werft/secrets/github_app_key` are set
  (otherwise onboarding 503s below).
- `/opt/werft/config/dnsmasq.d/dns-allow.conf` was generated from the same
  `dispatch.json` the project uses, and `dns-guard` was restarted after it.

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
grep -E 'query\[' dns-guard.log             # the agent asked
grep -E '^.*forwarded ' dns-guard.log       # dns-guard reached its upstream
grep -E '^.*reply .* is ' dns-guard.log     # and got an answer back
```

Expected: `squid-access.log` contains at least one line for a host the
run's `egress_hosts()` allowlist covers (e.g. `github.com` or
`api.anthropic.com`), timestamped inside the run's `claimed`..terminal
window.

For `dns-guard.log`, `query[...]` alone is **not** sufficient to pass this
stage — dnsmasq logs the query it received even when it has no route to its
upstream and answers nothing. `dnsmasq.conf` sets `log-queries=extra`, so a
resolver that actually worked writes all three shapes for the same name:

```
query[A] github.com from 10.90.0.5
forwarded github.com to 1.1.1.1
reply github.com is 140.82.121.4
```

Both the `forwarded … to <upstream>` line and the `reply … is <addr>` line
must be present for an allowlisted host. Queries with no matching
`forwarded`/`reply` pair mean dns-guard is inert — the usual cause is the
container missing the `internet` network (`server=/<host>/1.1.1.1` is
unreachable from an `internal: true` network alone); check
`docker inspect werft-dns-guard --format '{{json .NetworkSettings.Networks}}'`
lists both `werft_mgr_egress` and `werft_internet`. A name that is *not*
allowlisted shows a `query[...]` with **no** `forwarded` line and a
`config <name> is NXDOMAIN` line instead (the `address=/#/` catch-all
answering locally) — that is the guard working, and it is worth
spot-checking one such name to prove the deny arm too:

```sh
grep -E 'config .* is NXDOMAIN' dns-guard.log
```

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

# ntfy_url — optional, read directly by both backup scripts for failure
# alerts (this is NOT compose's WERFT_NTFY_URL; without it they stay silent)
echo "https://ntfy.example.com/werft-backup" | sudo tee /opt/werft/secrets/ntfy_url >/dev/null
sudo chmod 0600 /opt/werft/secrets/ntfy_url
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
`/srv/werft/runs`. Three things are deliberately excluded (D9 and its T9
amendment): `/opt/werft/secrets/github_app_key` (re-creatable from GitHub
in minutes), `/srv/werft/runs/*/secrets` and `/srv/werft/runs/*/task.json`
— a run that is *live* at 03:30 still has its GitHub installation token and
`CLAUDE_CODE_OAUTH_TOKEN` in plaintext there (the scrub only happens at
teardown), and neither belongs offsite. Confirm with:

```sh
restic -r "$(cat /opt/werft/secrets/restic_repo)" ls latest | grep -E 'runs/.*/(secrets|task\.json)'
```

Expected: no output.

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
5. **Permission denied on a bind mount: suspect the SELinux label first,
   ownership second.** The host runs Enforcing with dockerd
   `selinux-enabled: true`, so an unlabelled host path is denied outright
   regardless of its uid/mode. `install.sh` sets `container_file_t` on
   `/srv/werft` and `/opt/werft/config` (`semanage fcontext` +
   `restorecon -R`) and `compose.yaml` carries `:z` on the small shared
   mounts — check `ls -Zd <path>` and `sudo ausearch -m avc -ts recent`
   before anything else. *Then* check ownership: `/srv/werft/egress/log/
   squid` must be writable by uid 23 (the squid image's in-container
   `squid` user) and `/srv/werft/egress/log/dnsguard` by uid 999
   (`create_layout` does this via `chown`). Empty `access.log`/`queries.log`
   after `up` is usually one of these two, in that order.
6. `/opt/werft/config/dnsmasq.d` is bind-mounted into `dns-guard` as a
   **directory** (`/etc/dnsmasq.d`), not as a single file: `gen-dns-allow.sh`
   publishes `dns-allow.conf` with an atomic `mv`, so the file gets a new
   inode every regeneration and a single-file mount would pin the old one
   forever. Two consequences: if the directory is missing at the first
   `docker compose up`, Docker creates an empty one and `dns-guard` fails on
   its missing `conf-file` (`install.sh`'s `create_layout` +
   `ensure_dns_allow_conf` create both, so a normal Stage 2 install never
   hits this); and a regenerated allowlist takes effect only after
   `docker compose -f /opt/werft/compose/compose.yaml restart dns-guard` —
   dnsmasq does **not** re-read its config on `SIGHUP`. Re-create a deleted
   file with
   `deploy/dns-guard/gen-dns-allow.sh /opt/werft/config/dispatch.json /opt/werft/config/dnsmasq.d/dns-allow.conf`,
   not by hand (an empty file starts, but resolves nothing).
7. `dns-guard` is on the `internet` network as well as `mgr_egress`. That is
   load-bearing, not incidental: the allowlist is a set of
   `server=/<host>/<upstream>` forwards, and `mgr_egress` is
   `internal: true`, so on that network alone the upstream is unreachable
   and the guard resolves nothing at all. It is still not an egress hole —
   dnsmasq speaks only DNS, and every name outside the allowlist is
   `NXDOMAIN` via the `address=/#/` catch-all.
8. `WERFT_QUOTA_CEILING_SECONDS` is not set by `compose.yaml` and defaults
   to `0`, at which the dispatch plane never claims a run (SPEC §7: no
   ceiling, no reservation). Stage 3 sets it explicitly. A manager that
   boots with it unset logs `app.dispatch_disabled_no_quota_ceiling` once
   and then looks perfectly healthy while never dispatching anything.
