from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Manager process configuration. One loading path (lineage A§5.5)."""

    model_config = SettingsConfigDict(env_prefix="WERFT_")

    database_url: str = "postgresql+asyncpg://werft:werft@localhost:5432/werft"

    # GitHub App auth (SPEC §6): the App's REST base URL, its OAuth client ID
    # (the JWT `iss` claim), and the ro-mounted file holding the App's RS256
    # private key PEM. Empty defaults mean "not configured" — the composition
    # root (app.py) only starts the GitHub poller loops when the client id is set.
    github_api_url: str = "https://api.github.com"
    github_app_client_id: str = ""
    github_app_private_key_file: str = ""

    # Orchestrator poll cadences (SPEC §3.3 item 5: "the 15 s reconciliation
    # tick is correctness"; SPEC §6.2: "issues 60 s, PR/check status 30 s").
    tick_seconds: int = 15
    issue_poll_seconds: int = 60
    check_poll_seconds: int = 30

    # Operator API auth (SPEC §9: "Static bearer token, Tailscale-only, TLS
    # via `tailscale cert`"). The ro-mounted file holding the static bearer
    # token (SPEC §10: secrets are file mounts, never env). Empty default
    # means "not configured" — `werft/api/auth.py` fails closed (403) rather
    # than treating an unmounted secret as "auth disabled".
    api_token_file: str = ""

    # SPEC §8: artifact metadata lives in the DB, bytes on disk under
    # `{artifacts_root}/{run_id}/artifacts/` — the root the collector writes
    # to and `werft/api/routes.py`'s artifact-download route reads from.
    artifacts_root: str = "/srv/werft/runs"

    # Alerts (SPEC §9.5): `NtfyAlertSink`'s publish target. An empty
    # `ntfy_url` means "not configured" — the composition root (app.py)
    # keeps `NullAlertSink` until it's set. The ro-mounted file holding the
    # ntfy access token (SPEC §10: secrets are file mounts, never env);
    # empty/missing means "no Authorization header" (a private topic on a
    # self-hosted ntfy instance may not require one).
    ntfy_url: str = ""
    ntfy_topic: str = "werft"
    ntfy_token_file: str = ""

    # SPEC §9 operator surface, static-serving half (B7): the built Svelte
    # dashboard's `dist/` directory. Empty (the default) means "not built /
    # not deployed yet" — the composition root (app.py) mounts it at `/ui`
    # only when this names a directory that actually exists at `create_app`
    # time, so a manager deployed without a built dashboard still boots.
    dashboard_dist: str = ""

    # --- T7 dispatch plane (SPEC §4.2/§4.3) --------------------------------
    #: The per-run directory tree: `{runs_root}/{run_id}/{workspace,outputs,
    #: secrets,task.json}`. Shares the default with `artifacts_root` on
    #: purpose — T8's collector writes `{run_id}/artifacts/`, a sibling name
    #: dispatch never creates. `create_app` warns when the two diverge.
    runs_root: str = "/srv/werft/runs"
    #: SPEC §4.5: per-project runner config is Werft config, never repo config
    #: (an agent can edit the repo). Empty means "nothing dispatchable yet".
    dispatch_config_file: str = ""
    #: SPEC §4.4: the manager-held provider credential (`claude setup-token`
    #: output), read manager-side by `ClaudeSpec.build_env`. A ro file mount.
    claude_credential_file: str = ""
    #: Clone origin. `github_api_url` is the REST host; this is the git host.
    github_web_url: str = "https://github.com"
    #: SPEC §10: the manager reaches the daemon through docker-socket-proxy.
    docker_url: str = "unix:///var/run/docker.sock"
    #: SPEC §4.2: the per-run network's dns-guard address (T9 provisions it).
    runner_dns_ip: str = "127.0.0.11"
    runner_entrypoint: list[str] = Field(
        default_factory=lambda: ["/opt/werft/adapter/bin/werft-adapter"]
    )
    #: The VM-shaped bound quota admission does not express: the ceiling
    #: bounds provider-time, not RAM. Enforced inside the claim transaction,
    #: under the advisory lock (plan decision D13).
    max_concurrent_runs: int = 2
    dispatch_max_claims_per_tick: int = 4
    #: SPEC §3.3 item 4: the long wait lives in these columns. A live driver
    #: renews the lease every `heartbeat_seconds`; the sweeps only ever touch
    #: a row whose lease has already expired *and* which no live driver owns.
    lease_seconds: int = 120
    heartbeat_seconds: int = 30
    #: `hard_deadline_at = claim + timeout_seconds + this` — the margin covers
    #: clone, image start and teardown around the CLI's own ceiling, so the
    #: deadline sweep is a crash-only backstop and never races a live driver.
    hard_deadline_grace_seconds: int = 600
    #: Bounded drain for live per-run driver tasks at shutdown. Containers are
    #: deliberately left running (plan decision D1).
    driver_drain_seconds: float = 10.0
    #: SPEC §4.4: "re-minted by rename before expiry". Installation tokens
    #: live an hour; re-mint with this much validity left.
    token_remint_margin_seconds: int = 600
    #: `log.jsonl` is agent-written and unbounded; the result envelope is its
    #: last line, so a bounded tail read is enough to classify (decision 16).
    log_tail_bytes: int = 4 * 1024 * 1024
    clone_timeout_seconds: float = 600.0

    # --- SPEC §7 quota self-cap -------------------------------------------
    quota_provider: str = "claude"
    quota_account_label: str = "primary"
    #: SPEC §7's one knob. 0 means "not configured": no account row is
    #: reconciled and nothing dispatches (no invented ceilings). Applied as an
    #: upsert at startup, so config + restart is the whole operator surface.
    quota_ceiling_seconds: int = 0
    quota_rolling_window_hours: int = 5
    quota_window_cap_runs: int | None = None
    quota_window_capacity_seconds: int | None = None
    #: Used only where ageing can never produce headroom, or where a
    #: non-convertible provider reading blocks: a retry interval, deliberately
    #: not dressed up as a provider fact.
    quota_block_fallback_seconds: int = 900
    #: Floor under the `queued -> blocked_quota` wake, so a stale
    #: `exhausted_until` in the past cannot spin the tick.
    blocked_quota_floor_seconds: int = 60
    #: The reservation `earliest_headroom_at` asks about when no specific
    #: project is in hand (the `advance_failed` wake path).
    typical_reservation_seconds: int = 5400
