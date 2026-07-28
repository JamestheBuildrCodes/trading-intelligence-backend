import hashlib
import secrets

import jwt
from fastapi import HTTPException, status

from app.config import get_settings


def decode_supabase_jwt(token: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired session token: {exc}",
        ) from exc
    return payload


def generate_connector_token() -> tuple[str, str]:
    """Returns (raw_token, token_hash). Only the hash is stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_connector_token(raw)


def hash_connector_token(raw_token: str) -> str:
    settings = get_settings()
    salted = f"{settings.connector_token_pepper}:{raw_token}"
    return hashlib.sha256(salted.encode()).hexdigest()
