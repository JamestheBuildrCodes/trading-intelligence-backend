import hashlib
import secrets

from app.config import get_settings


def generate_connector_token() -> tuple[str, str]:
    """Return (raw_token, token_hash). Only the hash is stored in Supabase."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_connector_token(raw)


def hash_connector_token(raw_token: str) -> str:
    settings = get_settings()
    salted = f"{settings.connector_token_pepper}:{raw_token}"
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()
