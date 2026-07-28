from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from supabase import Client

from app.db import get_supabase
from app.security import decode_supabase_jwt, hash_connector_token


class CurrentUser:
    def __init__(self, id: str, email: str | None):
        self.id = id
        self.email = email


def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    payload = decode_supabase_jwt(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject")
    return CurrentUser(id=user_id, email=payload.get("email"))


def require_account_owner(
    account_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Client = Depends(get_supabase),
) -> str:
    """
    Verifies the account belongs to the requesting user. Required on every
    account-scoped route since the backend uses the service-role key, which
    bypasses RLS — this check is the substitute for it at this layer.
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
    """
    Authenticates the read-only MT5 bridge/EA. Returns the account_id the
    token is scoped to. Updates last_used_at as a side effect so the
    dashboard's "connection status" reflects real recent activity.
    """
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
