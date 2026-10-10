from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from supabase import Client

from app.db import get_supabase
from app.security import hash_connector_token


class CurrentUser:
    def __init__(self, id: str, email: str | None):
        self.id = id
        self.email = email


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Client = Depends(get_supabase),
) -> CurrentUser:
    """
    Validate the bearer token with Supabase Auth itself.

    This supports projects using legacy HS256 JWT signing or newer asymmetric
    signing keys. The backend never trusts an unverified subject claim.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing bearer token", headers={"WWW-Authenticate": "Bearer"})

    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Missing bearer token", headers={"WWW-Authenticate": "Bearer"})

    try:
        response = db.auth.get_user(token.strip())
        auth_user = getattr(response, "user", None)
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if not auth_user or not getattr(auth_user, "id", None):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return CurrentUser(id=str(auth_user.id), email=getattr(auth_user, "email", None))


def require_account_owner(
    account_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Client = Depends(get_supabase),
) -> str:
    """
    Verify account ownership for every account-scoped endpoint. The backend
    uses a service-role client, which bypasses RLS, so this check is mandatory.
    """
    result = (
        db.table("trading_accounts")
        .select("id")
        .eq("id", account_id)
        .eq("user_id", user.id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Account not found")
    return account_id


def require_connector_token(
    x_connector_token: str | None = Header(default=None),
    db: Client = Depends(get_supabase),
) -> str:
    """Authenticate the MT5 bridge and return the token's scoped account ID."""
    if not x_connector_token:
        raise HTTPException(status_code=401, detail="Missing X-Connector-Token header")

    token_hash = hash_connector_token(x_connector_token)
    result = (
        db.table("connector_tokens")
        .select("id, account_id, expires_at, revoked_at")
        .eq("token_hash", token_hash)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=401, detail="Invalid connector token")

    row = result.data[0]
    if row.get("revoked_at"):
        raise HTTPException(status_code=401, detail="Connector token revoked")

    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Connector token expired")

    db.table("connector_tokens").update(
        {"last_used_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", row["id"]).execute()

    return row["account_id"]
