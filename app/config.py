from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    # Server-only key. Never expose this as a NEXT_PUBLIC_* variable.
    supabase_service_role_key: str
    # Stable, random secret used to hash MT5 connector tokens. Set it explicitly.
    connector_token_pepper: str
    ai_daily_budget_usd: float = 1.0
    allowed_origins: str = "http://localhost:3000"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
