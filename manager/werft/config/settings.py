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
