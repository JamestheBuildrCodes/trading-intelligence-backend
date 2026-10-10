from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    # Prefer the current server-only key. Keep the legacy field for migration.
    supabase_secret_key: str | None = None
    supabase_service_role_key: str | None = None
    # Required: don't silently hash connector tokens with a public default.
    connector_token_pepper: str = Field(min_length=32)
    ai_daily_budget_usd: float = 1.0
    allowed_origins: str = "http://localhost:3000"

    @model_validator(mode="after")
    def require_server_key(self):
        if not (self.supabase_secret_key or self.supabase_service_role_key):
            raise ValueError(
                "Set SUPABASE_SECRET_KEY (recommended) or the legacy "
                "SUPABASE_SERVICE_ROLE_KEY in the server environment"
            )
        return self

    @property
    def supabase_admin_key(self) -> str:
        key = self.supabase_secret_key or self.supabase_service_role_key
        if not key:
            raise ValueError("A Supabase server-only key is required")
        return key

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
