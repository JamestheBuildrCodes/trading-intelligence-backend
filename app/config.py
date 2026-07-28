from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    supabase_service_role_key: str
    # Supabase's legacy shared JWT secret (Project Settings → API → JWT Settings).
    # If your project has migrated to asymmetric signing keys, this approach
    # needs to change to JWKS verification — check your project's settings.
    supabase_jwt_secret: str
    connector_token_pepper: str = "change-me"
    ai_daily_budget_usd: float = 1.0
    allowed_origins: str = "http://localhost:3000"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
