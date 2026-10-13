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


def _tickerall_token() -> str:
    token = get_settings().tickerall_api_key
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Hosted MT5 is not enabled yet. The service operator must configure TICKERALL_API_KEY in Render.",
        )
    return token


@router.post("/{account_id}/hosted-mt5", response_model=HostedMt5ConnectOut, status_code=status.HTTP_202_ACCEPTED)
async def connect_hosted_mt5(
    body: HostedMt5ConnectIn,
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    """Create a read-only hosted TickerAll session. Broker passwords are never persisted by Tradvoryx."""
    current = _account_row(db, account_id)
    require_account_feature(db, account_id, "hosted_mt5")
    provider = body.provider
    if current.get("connection_provider") and current.get("provider_account_id"):
        raise HTTPException(
            status_code=409,
            detail="This Tradvoryx account already has a hosted connection. Refresh its status or disconnect it before reconnecting.",
        )
    if not body.login.isdigit():
        raise HTTPException(status_code=422, detail="The MT5 login must contain only the numeric broker account number.")

    import httpx
    remote = {}
    provider_id = ""
    if provider == "tickerall":
        token = _tickerall_token()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0)) as client:
                response = await client.post(
                    "https://api.tickerall.com/v1/sessions",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={
                        "broker": "mt5",
                        "server": body.server,
                        "account": int(body.login),
                        "password": body.investor_password,
                        "terminalType": "MOBILE",
                    },
                )
            if response.status_code >= 400:
                provider_error = {}
                try:
                    provider_error = response.json()
                except Exception:
                    pass
                code = str(provider_error.get("error") or "tickerall_session_failed")
                safe_messages = {
                    "UNAUTHORIZED": "TickerAll rejected the API key. Check TICKERALL_API_KEY in Render.",
                    "BROKER_AUTH_FAILED": "The broker rejected the MT5 login/password/server combination. Confirm the exact server and investor/read-only password.",
                    "BROKER_UNREACHABLE": "TickerAll could not reach the broker. Retry shortly and verify the broker server.",
                    "FREE_TIER_LIVE_REJECTED": "TickerAll's current plan does not permit this live account through the API. Check the plan requirements before retrying.",
                }
                raise HTTPException(
                    status_code=502,
                    detail={"code": code if code in safe_messages else "tickerall_session_failed",
                            "message": safe_messages.get(code, "TickerAll could not establish the hosted MT5 session. Verify the broker server and account credentials, then retry.")},
                )
            remote = response.json()
            provider_id = str(remote.get("accountId") or "").strip()
            if not provider_id:
                raise HTTPException(status_code=502, detail={"code": "tickerall_invalid_response", "message": "TickerAll did not return a hosted account ID. No connection was saved."})
            provider_status = str(remote.get("status") or "connected").lower()
            mapped_status = "connected" if provider_status in {"connected", "online"} else "connecting"
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=502,
                detail={"code": "tickerall_unreachable", "message": "Tradvoryx could not reach TickerAll. Retry shortly; no broker password was saved."},
            ) from None
    elif provider == "metaapi":
        token = get_settings().metaapi_token
        if not token:
            raise HTTPException(status_code=503, detail={"code": "metaapi_not_configured", "message": "MetaApi is not enabled on this deployment. The operator must configure METAAPI_TOKEN in Render."})
        api = None
        remote_account = None
        try:
            from metaapi_cloud_sdk import MetaApi
            api = MetaApi(token)
            remote_account = await api.metatrader_account_api.create_account({
                "name": f"Tradvoryx {str(current.get('label') or 'MT5 account')}"[:100],
                "type": "cloud",
                "login": body.login,
                "password": body.investor_password,
                "server": body.server,
                "platform": "mt5",
                "application": "Tradvoryx",
                "magic": 0,
            })
            provider_id = str(getattr(remote_account, "id", "") or "").strip()
            if not provider_id:
                raise RuntimeError("metaapi_account_id_missing")
            await remote_account.deploy()
            mapped_status = "provisioning"
        except HTTPException:
            raise
        except Exception:
            if remote_account is not None:
                try:
                    await remote_account.remove()
                except Exception:
                    pass
            raise HTTPException(
                status_code=502,
                detail={"code": "metaapi_provisioning_failed", "message": "MetaApi could not provision this MT5 account. Check MetaApi workspace capacity, broker server spelling, and account credentials; no broker password was saved by Tradvoryx."},
            ) from None
    else:
        raise HTTPException(status_code=422, detail="Unsupported hosted MT5 provider.")

    try:
        db.table("trading_accounts").update({
            "connection_provider": provider,
            "provider_account_id": provider_id,
            "hosted_connection_status": mapped_status,
            "hosted_last_error_code": None,
            "hosted_history_cursor_at": None,
            "hosted_initial_backfill_complete": False,
            "hosted_first_deal_at": None,
            "hosted_last_deal_at": None,
            "hosted_connection_verified_at": datetime.now(timezone.utc).isoformat() if mapped_status == "connected" else None,
            "hosted_last_sync_at": None,
            "broker": current.get("broker") or "MT5",
            "server": body.server,
            "mt5_login": body.login,
            "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", account_id).execute()
    except Exception:
        # Best-effort cleanup if Tradvoryx cannot persist the connection reference.
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.delete(
                    f"https://api.tickerall.com/v1/sessions/{provider_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="TickerAll connected the broker, but Tradvoryx could not save the connection state. Contact support before retrying.") from None

    return {
        "account_id": account_id,
        "provider": provider,
        "status": mapped_status,
        "provider_account_id": provider_id,
        "message": ("TickerAll connected the MT5 account. Next, run the hosted history sync to import trades and verify account data." if provider == "tickerall" else "MetaApi accepted the account and started provisioning. Run the MetaApi history sync after the account becomes connected."),
    }


@router.get("/{account_id}/hosted-mt5/status")
def hosted_mt5_status(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    row = _account_row(db, account_id)
    worker_health = _worker_health(db, account_id)
    provider_enabled = (bool(get_settings().tickerall_api_key) if row.get("connection_provider") in (None, "tickerall") else bool(get_settings().metaapi_token))
    return {
        "account_id": account_id,
        "provider": row.get("connection_provider"),
        "provider_enabled": provider_enabled,
        "available_providers": [p for p, enabled in (("tickerall", bool(get_settings().tickerall_api_key)), ("metaapi", bool(get_settings().metaapi_token))) if enabled],
        "worker_ready": worker_health["worker_ready"],
        "worker_status": worker_health["worker_status"],
        "worker_last_completed_at": worker_health["worker_last_completed_at"],
        "worker_age_seconds": worker_health["worker_age_seconds"],
        "configured": bool(row.get("connection_provider") in ("metaapi", "tickerall") and row.get("provider_account_id")),
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
            if row.get("connection_provider") in ("metaapi", "tickerall") and row.get("provider_account_id")
            else "No hosted MT5 connection is configured for this account."
        ),
    }


@router.post("/{account_id}/hosted-mt5/disconnect")
async def disconnect_hosted_mt5(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    row = _account_row(db, account_id)
    if row.get("connection_provider") in ("metaapi", "tickerall") and row.get("provider_account_id"):
        require_account_feature(db, account_id, "hosted_mt5")
    provider_id = row.get("provider_account_id")
    if row.get("connection_provider") == "tickerall" and provider_id:
        token = _tickerall_token()
        import httpx
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.delete(
                    f"https://api.tickerall.com/v1/sessions/{provider_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if response.status_code not in (200, 204):
                raise RuntimeError("tickerall_disconnect_failed")
        except Exception:
            db.table("trading_accounts").update({
                "hosted_connection_status": "error",
                "hosted_last_error_code": "provider_disconnect_failed",
            }).eq("id", account_id).execute()
            raise HTTPException(status_code=502, detail="TickerAll did not confirm disconnection. The connection remains recorded so it can be retried safely.") from None
    elif row.get("connection_provider") == "metaapi" and provider_id:
        token = get_settings().metaapi_token
        if not token:
            raise HTTPException(status_code=503, detail="This is a legacy MetaApi connection. Configure METAAPI_TOKEN temporarily to disconnect it safely, or contact support.")
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
