"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "cloud-usage-metering"
    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./metering.db"
    api_key: str | None = None

    default_currency: str = "BDT"
    rating_granularity: Literal["hour", "day"] = "hour"
    max_batch_size: int = 1000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
