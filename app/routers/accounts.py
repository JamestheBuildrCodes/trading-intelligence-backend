from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from supabase import Client

from app.db import get_supabase
from app.deps import CurrentUser, get_current_user, require_account_owner
from app.schemas import AccountCreate, AccountOut, AccountSettingsUpdate, ConnectorTokenOut
from app.security import generate_connector_token
from app.services.entitlements import get_user_entitlements

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountOut])
def list_accounts(
    user: CurrentUser = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    result = db.table("trading_accounts").select("*").eq("user_id", user.id).execute()
    return result.data


@router.post("", response_model=AccountOut)
def create_account(
    body: AccountCreate,
    user: CurrentUser = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    entitlement = get_user_entitlements(db, user.id)
    max_accounts = entitlement["entitlements"]["max_accounts"]
    owned_result = db.table("trading_accounts").select("id").eq("user_id", user.id).execute()
    owned_count = len(owned_result.data or [])
    if max_accounts is not None and owned_count >= int(max_accounts):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "plan_limit_reached",
                "message": f"Your {entitlement['plan'].title()} plan allows {max_accounts} active trading account(s). Remove an account or upgrade your plan to add another.",
                "plan": entitlement["plan"],
                "max_accounts": max_accounts,
                "current_accounts": owned_count,
            },
        )

    row = body.model_dump()
    row["user_id"] = user.id
    row["current_balance"] = body.initial_balance
    row["current_equity"] = body.initial_balance
    result = db.table("trading_accounts").insert(row).execute()
    return result.data[0]



@router.patch("/{account_id}", response_model=AccountOut)
def update_account_settings(
    body: AccountSettingsUpdate,
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    """Update account label, timezone and advisory risk preferences."""
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Provide at least one setting to update.")

    result = (
        db.table("trading_accounts")
        .update(updates)
        .eq("id", account_id)
        .execute()
    )
    if not result.data:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Account not found.")
    return result.data[0]


@router.delete("/{account_id}")
def delete_account(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    """Permanently delete an owned account and its database-linked history.

    Hosted MetaApi accounts must be disconnected first so the provider resource
    is not orphaned. Related records are removed only where the schema defines
    cascading foreign keys.
    """
    from fastapi import HTTPException

    result = (
        db.table("trading_accounts")
        .select("id,connection_provider,provider_account_id")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Account not found.")

    account = result.data[0]
    if account.get("connection_provider") == "metaapi" and account.get("provider_account_id"):
        raise HTTPException(
            status_code=409,
            detail="Disconnect this hosted MT5 account before deleting it. This prevents leaving a cloud provider account running.",
        )

    db.table("trading_accounts").delete().eq("id", account_id).execute()
    return {"account_id": account_id, "deleted": True}


@router.post("/{account_id}/connector-tokens", response_model=ConnectorTokenOut)
def create_connector_token(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    raw_token, token_hash = generate_connector_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=90)
    db.table("connector_tokens").insert(
        {
            "account_id": account_id,
            "token_hash": token_hash,
            "expires_at": expires_at.isoformat(),
        }
    ).execute()
    # The raw token is returned exactly once — it is not retrievable again
    # after this response. Paste it straight into the EA/bridge config.
    return {"token": raw_token, "expires_at": expires_at}


@router.delete("/{account_id}/connector-tokens/{token_id}")
def revoke_connector_token(
    token_id: str,
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    db.table("connector_tokens").update(
        {"revoked_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", token_id).eq("account_id", account_id).execute()
    return {"revoked": True}
