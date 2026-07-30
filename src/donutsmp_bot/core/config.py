from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, PositiveFloat, PositiveInt, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    discord_bot_token: str = ""
    donut_api_base_url: str = "https://api.donutsmp.net/"
    database_url: str = "sqlite+aiosqlite:///./donutsmp.db"
    token_encryption_key: str = ""

    manifest_path: Path = Path("manifest_detailed.json")
    assets_dir: Path = Path(".")

    safe_requests_per_minute: PositiveInt = 220
    reserved_requests_per_minute: PositiveInt = 30
    default_poll_interval_seconds: PositiveFloat = 3
    max_search_pages: PositiveInt = 3
    request_timeout_seconds: PositiveFloat = 10
    default_hysteresis_percent: float = Field(default=2, ge=0, le=100)
    default_notification_cooldown_seconds: PositiveInt = 60
    observation_retention_days: PositiveInt = 7
    log_level: str = "INFO"

    @field_validator("donut_api_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.rstrip("/") + "/"

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @model_validator(mode="after")
    def validate_api_budget(self) -> Self:
        if self.safe_requests_per_minute + self.reserved_requests_per_minute > 250:
            raise ValueError(
                "SAFE_REQUESTS_PER_MINUTE + RESERVED_REQUESTS_PER_MINUTE "
                "must not exceed the DonutSMP limit of 250"
            )
        return self

    def validate_runtime_secrets(self) -> None:
        missing = []
        if not self.discord_bot_token:
            missing.append("DISCORD_BOT_TOKEN")
        if not self.token_encryption_key:
            missing.append("TOKEN_ENCRYPTION_KEY")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
