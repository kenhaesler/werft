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
