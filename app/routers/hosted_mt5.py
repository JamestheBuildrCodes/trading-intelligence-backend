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


def _worker_health(db: Client, account_id: str) -> dict:
    """Require a recent completed worker run before advertising cloud sync readiness."""
    try:
        result = (
            db.table("hosted_mt5_worker_runs")
            .select("started_at,completed_at,status,accounts_seen,accounts_succeeded,accounts_failed,safe_error_code")
            .eq("account_id", account_id)
            .order("started_at", desc=True)
            .limit(1)
            .execute()
        )
        row = result.data[0] if result.data else None
        if row is None or not row.get("completed_at"):
            return {
                "worker_ready": False,
                "worker_status": "never_ran",
                "worker_last_completed_at": row.get("completed_at") if row else None,
                "worker_age_seconds": None,
                "worker_error_code": row.get("safe_error_code") if row else None,
            }
        finished = datetime.fromisoformat(str(row["completed_at"]).replace("Z", "+00:00"))
        if finished.tzinfo is None or finished.utcoffset() is None:
            finished = finished.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - finished.astimezone(timezone.utc)).total_seconds()))
        ready = row.get("status") in ("succeeded", "partial") and age <= 900
        return {
            "worker_ready": ready,
            "worker_status": row.get("status") if age <= 900 else "stale",
            "worker_last_completed_at": finished.astimezone(timezone.utc).isoformat(),
            "worker_age_seconds": age,
            "worker_error_code": row.get("safe_error_code"),
        }
    except Exception:
        # Missing migration/table/permissions means not ready, never optimistic.
        return {
            "worker_ready": False,
            "worker_status": "health_unavailable",
            "worker_last_completed_at": None,
            "worker_age_seconds": None,
            "worker_error_code": "worker_health_unavailable",
        }


def _provider_token() -> str:
    token = get_settings().metaapi_token
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Hosted MT5 is not enabled yet. The service operator must configure METAAPI_TOKEN and deploy the hosted sync worker.",
        )
    return token


@router.post("/{account_id}/hosted-mt5", status_code=status.HTTP_410_GONE)
async def connect_hosted_mt5(
    body: HostedMt5ConnectIn,
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    """Retire new MetaApi provisioning; use the free account-scoped MT5 bridge instead."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "code": "hosted_provider_retired",
            "message": (
                "New MetaApi hosted-account provisioning has been retired in Tradvoryx. "
                "Use the free read-only Windows MT5 bridge from the Connect page. "
                "A logged-in MT5 terminal and the bridge process must remain running to sync."
            ),
        },
    )


@router.get("/{account_id}/hosted-mt5/status")
def hosted_mt5_status(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    row = _account_row(db, account_id)
    worker_health = _worker_health(db, account_id)
    provider_enabled = bool(get_settings().metaapi_token)
    return {
        "account_id": account_id,
        "provider": row.get("connection_provider"),
        "provider_enabled": provider_enabled,
        "worker_ready": worker_health["worker_ready"],
        "worker_status": worker_health["worker_status"],
        "worker_last_completed_at": worker_health["worker_last_completed_at"],
        "worker_age_seconds": worker_health["worker_age_seconds"],
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
            "This account uses hosted MT5. Tradvoryx's browser does not need your desktop terminal."
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
