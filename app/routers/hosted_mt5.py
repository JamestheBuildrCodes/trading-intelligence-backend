from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.config import get_settings
from app.db import get_supabase
from app.deps import require_account_owner
from app.schemas import HostedMt5ConnectIn, HostedMt5ConnectOut
from app.services.entitlements import require_account_feature

router = APIRouter(prefix="/api/v1/accounts", tags=["hosted MT5"])


def _account_row(db: Client, account_id: str) -> dict:
    result = (
        db.table("trading_accounts")
        .select("id,label,broker,server,mt5_login,currency,connection_provider,provider_account_id,hosted_connection_status,hosted_last_error_code,hosted_history_cursor_at,hosted_first_deal_at,hosted_last_deal_at,hosted_connection_verified_at,hosted_last_sync_at")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Trading account not found.")
    return result.data[0]


def _provider_token() -> str:
    token = get_settings().metaapi_token
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Hosted MT5 is not enabled yet. The service operator must configure METAAPI_TOKEN and deploy the hosted sync worker.",
        )
    return token


@router.post("/{account_id}/hosted-mt5", response_model=HostedMt5ConnectOut, status_code=status.HTTP_202_ACCEPTED)
async def connect_hosted_mt5(
    body: HostedMt5ConnectIn,
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    """Provision an MT5 account on MetaApi. The investor password is never persisted by TradeGrid."""
    current = _account_row(db, account_id)
    require_account_feature(db, account_id, "hosted_mt5")
    if current.get("connection_provider") == "metaapi" and current.get("provider_account_id"):
        raise HTTPException(
            status_code=409,
            detail="This TradeGrid account already has a hosted provider connection. Refresh its status or disconnect it before reconnecting.",
        )

    token = _provider_token()
    try:
        from metaapi_cloud_sdk import MetaApi

        api = MetaApi(token)
        remote = await api.metatrader_account_api.create_account({
            "name": f"TradeGrid · {current.get('label') or 'MT5 account'}",
            "type": "cloud",
            "login": body.login,
            "password": body.investor_password,
            "server": body.server,
            "platform": "mt5",
            "application": "MetaApi",
            "magic": 730041,
            "reliability": "high",
        })
    except Exception as exc:
        # Never store/log provider messages; some errors may include user-supplied fields.
        code = getattr(exc, "details", None)
        safe_code = code if code in {"E_AUTH", "E_SRV_NOT_FOUND", "E_SERVER_TIMEZONE"} else "provider_provisioning_failed"
        raise HTTPException(
            status_code=502,
            detail={
                "code": safe_code,
                "message": (
                    "The broker rejected the supplied credentials. Use the MT5 investor/read-only password and verify login/server."
                    if safe_code == "E_AUTH"
                    else "The provider could not identify this broker server. Check the exact MT5 server name or ask support about a provisioning profile."
                    if safe_code == "E_SRV_NOT_FOUND"
                    else "Hosted MT5 provisioning did not complete. Verify the broker server and try again; no password was saved in TradeGrid."
                ),
            },
        ) from None

    try:
        provider_id = str(remote.id)
        db.table("trading_accounts").update({
            "connection_provider": "metaapi",
            "provider_account_id": provider_id,
            "hosted_connection_status": "provisioning",
            "hosted_last_error_code": None,
            "hosted_history_cursor_at": None,
            "hosted_initial_backfill_complete": False,
            "hosted_first_deal_at": None,
            "hosted_last_deal_at": None,
            "hosted_connection_verified_at": None,
            "hosted_last_sync_at": None,
            "broker": current.get("broker") or "MetaApi hosted MT5",
            "server": body.server,
            "mt5_login": body.login,
            "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", account_id).execute()
    except Exception:
        # Avoid orphaning the provider account if our persistence step fails.
        try:
            await remote.remove()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="The hosted account was created by the provider but TradeGrid could not save its connection state. Contact support before retrying.") from None

    return {
        "account_id": account_id,
        "provider": "metaapi",
        "status": "provisioning",
        "provider_account_id": provider_id,
        "message": "Hosted account provisioning started. The background worker will connect, import history and update the connection status.",
    }


@router.get("/{account_id}/hosted-mt5/status")
def hosted_mt5_status(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    row = _account_row(db, account_id)
    return {
        "account_id": account_id,
        "provider": row.get("connection_provider"),
        "provider_enabled": bool(get_settings().metaapi_token),
        "configured": bool(row.get("connection_provider") == "metaapi" and row.get("provider_account_id")),
        "status": row.get("hosted_connection_status") or "not_configured",
        "provider_account_id": row.get("provider_account_id"),
        "last_error_code": row.get("hosted_last_error_code"),
        "history_cursor_at": row.get("hosted_history_cursor_at"),
        "first_deal_at": row.get("hosted_first_deal_at"),
        "last_deal_at": row.get("hosted_last_deal_at"),
        "verified_at": row.get("hosted_connection_verified_at"),
        "last_sync_at": row.get("hosted_last_sync_at"),
        "message": (
            "This account uses hosted MT5. TradeGrid's browser does not need your desktop terminal."
            if row.get("connection_provider") == "metaapi" and row.get("provider_account_id")
            else "No hosted MT5 connection is configured for this account."
        ),
    }


@router.post("/{account_id}/hosted-mt5/disconnect")
async def disconnect_hosted_mt5(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    row = _account_row(db, account_id)
    if row.get("connection_provider") == "metaapi" and row.get("provider_account_id"):
        require_account_feature(db, account_id, "hosted_mt5")
    provider_id = row.get("provider_account_id")
    if row.get("connection_provider") == "metaapi" and provider_id:
        token = _provider_token()
        try:
            from metaapi_cloud_sdk import MetaApi

            api = MetaApi(token)
            remote = await api.metatrader_account_api.get_account(str(provider_id))
            await remote.undeploy()
            await remote.remove()
        except Exception:
            # Keep provider id so the operator can safely retry deprovisioning.
            db.table("trading_accounts").update({
                "hosted_connection_status": "error",
                "hosted_last_error_code": "provider_disconnect_failed",
            }).eq("id", account_id).execute()
            raise HTTPException(status_code=502, detail="The cloud provider did not confirm disconnection. The connection is still recorded so it can be retried safely.") from None

    db.table("trading_accounts").update({
        "connection_provider": None,
        "provider_account_id": None,
        "hosted_connection_status": "disconnected",
        "hosted_last_error_code": None,
        "hosted_history_cursor_at": None,
        "hosted_connection_verified_at": None,
        "hosted_last_sync_at": None,
        "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", account_id).execute()
    return {"account_id": account_id, "status": "disconnected", "provider_resources_removed": bool(provider_id)}
