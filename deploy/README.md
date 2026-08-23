# Werft deploy stack

`compose.yaml` is the whole runtime topology: six services, six networks,
file-mount secrets, no public listeners (SPEC §10). This document is the
operator's side of it — what every `.env` key means, how to resolve the
pinned image digests, the host directory contract the services assume
already exists, and how to roll out an egress allowlist change.

## Services

| Service | Role |
|---|---|
| `postgres` | The one writer's database (SPEC §3.3.1). PG 18, digest-pinned. |
| `manager-migrate` | Runs `alembic upgrade head` once, before `manager` starts, then exits (`restart: "no"`). |
| `manager` | The FastAPI app + orchestrator. Only container with a published port, and only on the Tailscale IP. |
| `docker-socket-proxy` | `tecnativa/docker-socket-proxy`, digest-pinned. The manager's only path to `/var/run/docker.sock` — a narrow, read-mostly permission set (see `compose.yaml`'s `docker-socket-proxy.environment`), never the raw socket. |
| `egress-proxy` | Werft-built squid image (`egress-proxy/Dockerfile`). Runner containers reach the internet only through here, gated per source subnet by `/etc/squid/allow/*.txt`. |
| `dns-guard` | Werft-built dnsmasq image (`dns-guard/Dockerfile`). Resolves only the names in `dns-allow.conf`; everything else is `NXDOMAIN`. Attached to `mgr_egress` **and** `internet` — the allowlisted names are forwarded to a real upstream (`server=/<host>/1.1.1.1`), which an internal-only network could never reach. |

## `.env` keys

Copy `.env.example` to `.env` next to `compose.yaml` and fill in:

| Key | Meaning |
|---|---|
| `TS_IP` | This host's Tailscale IPv4 address (`tailscale ip -4`). `manager`'s port 8420 is published bound to this address only — never `0.0.0.0` (SPEC §10: zero public listeners). |
| `POSTGRES_IMAGE` | `postgres:18.x-alpine@sha256:<digest>` — digest-pinned, never a floating tag. See "Resolving image digests" below. Use the PG **18** volume layout (`/var/lib/postgresql`, not the pre-18 `/data` path — SPEC §2). |
| `SOCKET_PROXY_IMAGE` | `tecnativa/docker-socket-proxy@sha256:<digest>` — digest-pinned. |
| `GITHUB_APP_CLIENT_ID` | The GitHub App's OAuth client ID (the JWT `iss` claim). Empty means "GitHub integration not configured" — the manager still boots, API-only, orchestrator dark. |
| `NTFY_URL` | Base URL of the ntfy instance alerts publish to (e.g. `https://ntfy.example.com`). Empty means "no alerts": `NullAlertSink` stays wired. |

`werft-manager` and `werft-egress-proxy`/`werft-dns-guard` are built locally
(`image: ...:local`) — they are not pulled, so they carry no digest pin in
`.env`; the Dockerfiles themselves pin their base images (`rockylinux/rockylinux@sha256:...`).

### Resolving image digests

```sh
docker buildx imagetools inspect postgres:18-alpine
docker buildx imagetools inspect tecnativa/docker-socket-proxy:latest
```

Each prints a `Digest: sha256:...` line (use the digest for your host's
architecture if the output lists more than one). Paste it into `.env` as
`postgres:18-alpine@sha256:<digest>` / `tecnativa/docker-socket-proxy@sha256:<digest>`.
Re-run and re-pin on every deliberate upgrade — never let compose float a tag.

## Manager environment (`WERFT_*`)

Every `WERFT_*` key in `compose.yaml`'s `manager`/`manager-migrate` services
is a field on `manager/werft/config/settings.py`'s `Settings`
(`env_prefix="WERFT_"` — the field name upper-cased is the env var name).
Notable ones:

| Env var | Settings field | Notes |
|---|---|---|
| `WERFT_DATABASE_URL` | `database_url` | Password-free template (`postgresql+asyncpg://werft@postgres:5432/werft`) — see below. |
| `WERFT_DATABASE_PASSWORD_FILE` | `database_password_file` | `/run/secrets/pg_password`. See "The database password seam". |
| `WERFT_DOCKER_URL` | `docker_url` | `tcp://docker-socket-proxy:2375` — the manager never touches the raw socket. |
| `WERFT_RUNS_ROOT` | `runs_root` | `/srv/werft/runs` — must equal `artifacts_root`'s default or the manager warns at boot. |
| `WERFT_DISPATCH_CONFIG_FILE` | `dispatch_config_file` | `/opt/werft/config/dispatch.json` — per-project runner config (SPEC §4.5); a broken file fails boot loudly (D3). |
| `WERFT_API_TOKEN_FILE` | `api_token_file` | `/run/secrets/api_token` — the static bearer token guarding `/api/v1` (SPEC §9). |
| `WERFT_GITHUB_APP_CLIENT_ID` | `github_app_client_id` | From `.env`'s `GITHUB_APP_CLIENT_ID`. |
| `WERFT_GITHUB_APP_PRIVATE_KEY_FILE` | `github_app_private_key_file` | `/run/secrets/github_app_key`. |
| `WERFT_CLAUDE_CREDENTIAL_FILE` | `claude_credential_file` | `/run/secrets/claude_credential` — `claude setup-token` output. |
| `WERFT_NTFY_URL` / `WERFT_NTFY_TOKEN_FILE` | `ntfy_url` / `ntfy_token_file` | From `.env`'s `NTFY_URL`; token at `/run/secrets/ntfy_token`. |
| `WERFT_EGRESS_SLOT_COUNT` | `egress_slot_count` | `4` here — must be `>=` `max_concurrent_runs` (that setting isn't set in compose; its `2` default satisfies this). |
| `WERFT_EGRESS_SUBNET_PREFIX` | `egress_subnet_prefix` | `"10.90"` — first two octets of the per-slot runner subnets (slot *K* = `10.90.K.0/24`, matching `squid.conf`'s `slotN_src` ACLs). Set explicitly even though it's also the default, for legibility next to `EGRESS_SLOT_COUNT`. |
| `WERFT_EGRESS_ALLOWLIST_DIR` | `egress_allowlist_dir` | `/srv/werft/egress/allow` — shared by bind mount with `egress-proxy`'s `/etc/squid/allow`. |
| `WERFT_SQUID_ACCESS_LOG` / `WERFT_DNS_GUARD_QUERY_LOG` | `squid_access_log` / `dns_guard_query_log` | SPEC §8 evidence collection paths; empty means "not deployed, collect nothing" — set here since this compose stack *does* deploy both. |

Not set in `compose.yaml` (left at their `Settings` defaults, an operator
concern rather than a deploy-stack one): `WERFT_QUOTA_CEILING_SECONDS` (SPEC
§7 — `0` means dispatch stays dark, no invented ceiling), `WERFT_TICK_SECONDS`
and the other poll cadences, `WERFT_MAX_CONCURRENT_RUNS`. Add them to the
`manager` service's `environment:` block, or to `.env` plus a new
interpolated key, if you need to change them from their code defaults.

## The database password seam

`database_url` is one connection-string field, and SPEC §10 forbids putting
a secret in an env var — so the password can never simply live in
`WERFT_DATABASE_URL`. Instead:

- `WERFT_DATABASE_URL` is a **password-free template**:
  `postgresql+asyncpg://werft@postgres:5432/werft`.
- `WERFT_DATABASE_PASSWORD_FILE` names a ro-mounted secret file
  (`/run/secrets/pg_password`, the same Docker secret `postgres` itself
  reads via `POSTGRES_PASSWORD_FILE`).
- `manager/werft/domain/db_url.py`'s `apply_password_file(url, password_file)`
  reads that file, strips it, and splices the password into the URL with
  SQLAlchemy's `make_url(...).set(password=...)`. It's called from two
  places, so both processes that touch the database resolve the password
  identically:
  - `manager`: synchronously in `create_app`, before the app is even
    constructed — a set-but-unreadable `WERFT_DATABASE_PASSWORD_FILE` is a
    `PermanentError` at boot (the operator is watching), not a confusing
    failure on the engine's first query.
  - `manager-migrate`: `werft/db/migrations/env.py`'s `_url()`, which reads
    `WERFT_DATABASE_URL`/`WERFT_DATABASE_PASSWORD_FILE` straight off the
    environment (it can't import `Settings` — the `db` layer may not import
    `config`, SPEC §1's layering) and applies the same splice.

An unset `WERFT_DATABASE_PASSWORD_FILE` (compose always sets it here) leaves
`database_url` untouched — the pre-seam behavior, still the default for
local/test use. A file that's *set* but missing, unreadable (e.g. a host
file mode or SELinux label that denies the read), or empty/whitespace-only
is a `PermanentError` at boot in all three cases — never a silently
passwordless URL that only fails once something tries to connect.

A note on what "secrets are file mounts, never env" buys and what it does
not: `deploy/manager/Dockerfile` declares no `USER`, so the manager process
runs as **root inside its container**. The posture is therefore about the
*host* side — `0700` on `/opt/werft/secrets`, `0600` on the files, and the
values never appearing in `docker inspect`/the process environment or in a
child process it spawns — not about an in-container uid boundary. The
`ro` mounts plus those host modes are the boundary; the container uid is
not one.

## `/srv/werft` and `/opt/werft`

Both trees are created by `install.sh` (Part C) before the stack is ever
brought up; nothing in `compose.yaml` creates them.

- **`/srv/werft`** — runtime state, mounted **same-path** into `manager`
  (`/srv/werft:/srv/werft`) because create-body mount sources are host paths
  (D7) — the manager writes a path like `/srv/werft/runs/<id>/workspace` into
  a container spec, and that path must resolve identically inside `manager`
  and inside the container Docker just started alongside it.
  - `runs/` — `WERFT_RUNS_ROOT`/`WERFT_ARTIFACTS_ROOT`: per-run
    workspace/outputs/secrets/task.json trees and collected artifacts.
  - `egress/allow/` — `WERFT_EGRESS_ALLOWLIST_DIR`, bind-mounted read-only
    into `egress-proxy` at `/etc/squid/allow`. Per-slot (`slot0.txt` …
    `slot3.txt`) and manager (`manager.txt`) squid `dstdomain` files.
  - `egress/log/squid/` — bind-mounted into `egress-proxy` at
    `/var/log/squid` (`WERFT_SQUID_ACCESS_LOG` points at `access.log` in it).
  - `egress/log/dnsguard/` — bind-mounted into `dns-guard` at
    `/var/log/dnsguard` (`WERFT_DNS_GUARD_QUERY_LOG` points at
    `queries.log` in it).
- **`/opt/werft`** — operator-authored config and secrets, mounted read-only.
  - `config/dispatch.json` — `WERFT_DISPATCH_CONFIG_FILE`; per-project
    runner config (registries, extra hosts, provider).
  - `config/dnsmasq.d/` — a **directory**, bind-mounted read-only into
    `dns-guard` at `/etc/dnsmasq.d`. It holds the one generated file,
    `dns-allow.conf` (by `gen-dns-allow.sh`, below). The mount is a
    directory rather than the single file deliberately: `gen-dns-allow.sh`
    writes a temp file and `mv`s it into place, which creates a **new
    inode**, and a single-file bind mount keeps serving the old one forever.
    **The directory and the file must both exist before the first
    `docker compose up`** — `install.sh` creates them (an empty
    `dns-allow.conf` is fine); if the directory is missing, Docker creates
    an empty one and `dns-guard` then fails on its missing
    `conf-file=/etc/dnsmasq.d/dns-allow.conf`.
  - `secrets/pg_password`, `secrets/api_token`, `secrets/github_app_key`,
    `secrets/claude_credential`, `secrets/ntfy_token` — the files
    `compose.yaml`'s top-level `secrets:` block points at. `install.sh`
    creates the `0700` directory; the operator writes the files (RUNBOOK
    Stage 3).
  - `secrets/restic_repo`, `secrets/restic_password`, `secrets/ntfy_url` —
    **not** compose secrets: host-side files read directly by
    `backup/werft-backup.sh` and `backup/werft-restore-drill.sh`
    (`RESTIC_REPOSITORY_FILE`, `RESTIC_PASSWORD_FILE`, and the optional
    failure-notification URL). `install.sh` never creates these — the
    operator must write them before the backup/restore-drill timers are of
    any use (RUNBOOK Stage 6). `ntfy_url` is optional; without it the
    scripts simply stay silent on failure.

### SELinux

The host runs Enforcing with dockerd `selinux-enabled: true` (both are
`install.sh` floors), so every bind-mounted host path needs a container
file label or the container sees `permission denied` on it.
`install.sh`'s `create_layout` sets the persistent default type
(`semanage fcontext -a -t container_file_t` for `/srv/werft(/.*)?` and
`/opt/werft/config(/.*)?`, then `restorecon -R`). `compose.yaml`
additionally carries `:z` on the small shared mounts (squid's two,
dns-guard's two, the manager's `/opt/werft/config`). `/srv/werft:/srv/werft`
is deliberately **not** `:z`-mounted: that would relabel the whole tree
recursively at every manager start and stomp the per-run workspace `:Z`
labels the runner create-bodies apply.

## Rolling out an egress allowlist change

The allowlist a project can reach through `egress-proxy` is derived from
`dispatch.json`'s per-project `registries`/`extra_hosts`. dns-guard's
allow-list is regenerated from the same source, so DNS and squid agree on
what's reachable. To change it:

1. Edit `/opt/werft/config/dispatch.json` — add/remove a project's
   `registries` preset (`npm`, `pypi`, `dnf-rocky`, `crates`, `go`) or an
   `extra_hosts` entry.
2. Regenerate `dns-allow.conf` from the updated config, into the
   bind-mounted `dnsmasq.d` directory:
   ```sh
   dns-guard/gen-dns-allow.sh /opt/werft/config/dispatch.json \
       /opt/werft/config/dnsmasq.d/dns-allow.conf
   ```
3. **Restart** dns-guard — not `kill -s HUP`:
   ```sh
   docker compose -f /opt/werft/compose/compose.yaml restart dns-guard
   ```
   Why a restart: dnsmasq does **not** re-read its configuration files on
   `SIGHUP` (HUP only re-reads `/etc/hosts`-style files and clears the
   cache); only a restart re-parses `conf-file`. And because
   `gen-dns-allow.sh` publishes its output with an atomic `mv`, the file
   gets a **new inode** — which is exactly why compose bind-mounts the
   `dnsmasq.d` *directory* rather than the single file: a single-file mount
   would pin the pre-`mv` inode and the container would never see the new
   content even across a restart.
4. squid's own per-slot/manager `dstdomain` files under
   `/srv/werft/egress/allow/` are rewritten by the manager itself at claim
   time (not by this script), and the manager sends
   `docker kill -s HUP werft-egress-proxy` after every rewrite (plan D2) —
   so no manual squid reload is part of this rollout. squid does **not**
   re-read those files per ACL check; it caches them in memory at
   (re)configure time. If you hand-edit `manager.txt` (the operator-owned
   one the manager never rewrites), you must HUP squid yourself:
   ```sh
   docker kill -s HUP werft-egress-proxy
   ```

`werft-dns-guard` and `werft-egress-proxy` are the fixed `container_name`s
`docker kill`/the manager's own HUP calls target — set in `compose.yaml`
specifically so those names survive a `docker compose up` recreate.

## Bringing the stack up

```sh
cd deploy
docker compose config -q      # validate before touching anything real
docker compose up -d --build
```

`manager-migrate` runs to completion (`alembic upgrade head`) before
`manager` starts (`depends_on: manager-migrate: condition:
service_completed_successfully`); `manager` additionally waits for
`docker-socket-proxy`, `egress-proxy`, and `dns-guard` to have started.
