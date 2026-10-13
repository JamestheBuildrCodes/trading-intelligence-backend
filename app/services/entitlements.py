from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.config import get_settings
from supabase import Client

PLAN_ENTITLEMENTS: dict[str, dict[str, Any]] = {
    "free": {"max_accounts": 1, "full_history_analytics": True, "advanced_cost_analysis": False, "portfolio_risk": False, "prop_firm_cockpit": False, "hosted_mt5": False, "ai_coach_monthly_messages": 0},
    "trader": {"max_accounts": 3, "full_history_analytics": True, "advanced_cost_analysis": True, "portfolio_risk": False, "prop_firm_cockpit": True, "hosted_mt5": False, "ai_coach_monthly_messages": 0},
    "pro": {"max_accounts": 10, "full_history_analytics": True, "advanced_cost_analysis": True, "portfolio_risk": True, "prop_firm_cockpit": True, "hosted_mt5": True, "ai_coach_monthly_messages": 0},
    "enterprise": {"max_accounts": None, "full_history_analytics": True, "advanced_cost_analysis": True, "portfolio_risk": True, "prop_firm_cockpit": True, "hosted_mt5": True, "ai_coach_monthly_messages": None},
}


def _subscription_row(db: Client, user_id: str) -> tuple[dict[str, Any] | None, bool]:
    """Read subscription data, falling back safely if the optional developer migration is absent.

    The fallback intentionally disables the developer override rather than granting it
    implicitly. This keeps account creation and ordinary paid-plan checks operational
    while a production database migration is pending.
    """
    table = db.table("user_subscriptions")
    try:
        response = (
            table.select("plan,status,billing_period,current_period_end,cancel_at_period_end,developer_access,developer_preview_plan")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        return (response.data[0] if response.data else None), True
    except Exception as exc:
        error_text = str(exc)
        missing_optional_columns = (
            ("developer_access" in error_text or "developer_preview_plan" in error_text)
            and ("42703" in error_text or "does not exist" in error_text)
        )
        if not missing_optional_columns:
            raise
        response = (
            table.select("plan,status,billing_period,current_period_end,cancel_at_period_end")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        return (response.data[0] if response.data else None), False


def get_user_entitlements(db: Client, user_id: str) -> dict[str, Any]:
    """Resolve subscription entitlements and the server-owned developer override."""
    row, developer_columns_available = _subscription_row(db, user_id)
    developer_access = bool(developer_columns_available and row and row.get("developer_access"))
    paid = bool(row and row.get("status") in ("active", "trialing"))
    period_end = row.get("current_period_end") if row else None
    if paid:
        if not period_end:
            paid = False
        else:
            try:
                parsed_end = period_end if isinstance(period_end, datetime) else datetime.fromisoformat(str(period_end).replace("Z", "+00:00"))
                if parsed_end.tzinfo is None or parsed_end.utcoffset() is None:
                    parsed_end = parsed_end.replace(tzinfo=timezone.utc)
                if parsed_end.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                    paid = False
            except (ValueError, TypeError):
                paid = False

    plan = str(row.get("developer_preview_plan") or "enterprise") if developer_access else (
        str(row.get("plan") or "free") if paid and row else "free"
    )
    if plan not in PLAN_ENTITLEMENTS:
        plan = "free"

    if developer_access:
        subscription_status = "developer"
    elif row and not paid and row.get("status") in ("active", "trialing"):
        subscription_status = "expired"
    else:
        subscription_status = row.get("status") if row else "inactive"

    return {
        "plan": plan,
        "subscription_status": subscription_status,
        "billing_period": row.get("billing_period") if paid and row and not developer_access else None,
        "current_period_end": row.get("current_period_end") if paid and row and not developer_access else None,
        "cancel_at_period_end": bool(row.get("cancel_at_period_end")) if paid and row and not developer_access else False,
        "paid": paid and not developer_access,
        "developer_access": developer_access,
        "developer_preview_plan": plan if developer_access else None,
        "entitlements": dict(PLAN_ENTITLEMENTS["enterprise"] if get_settings().beta_free_all_features else PLAN_ENTITLEMENTS[plan]),
        "beta_access": bool(get_settings().beta_free_all_features),
    }


def require_account_feature(db: Client, account_id: str, feature: str) -> dict[str, Any]:
    """Enforce a plan entitlement at the backend boundary for an owned account."""
    result = (
        db.table("trading_accounts")
        .select("id,user_id")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Account not found.")
    entitlement = get_user_entitlements(db, str(result.data[0]["user_id"]))
    beta_hosted_mt5 = feature == "hosted_mt5" and get_settings().hosted_mt5_beta_enabled
    allowed = True if beta_hosted_mt5 else entitlement["entitlements"].get(feature)
    if allowed is not True:
        required_label = {
            "advanced_cost_analysis": "Trader or Pro",
            "prop_firm_cockpit": "Trader or Pro",
            "portfolio_risk": "Pro",
            "hosted_mt5": "Pro",
        }.get(feature, "a higher plan")
        raise HTTPException(
            status_code=403,
            detail={
                "code": "plan_upgrade_required",
                "feature": feature,
                "current_plan": entitlement["plan"],
                "required_plan": required_label,
                "message": f"This feature requires the {required_label} plan. Your current plan is {entitlement['plan'].title()}.",
            },
        )
    return entitlement
