from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Manager process configuration. One loading path (lineage A§5.5)."""

    model_config = SettingsConfigDict(env_prefix="WERFT_")

    database_url: str = "postgresql+asyncpg://werft:werft@localhost:5432/werft"
