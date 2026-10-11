from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from supabase import Client

PLAN_ENTITLEMENTS: dict[str, dict[str, Any]] = {
    "free": {
        "max_accounts": 1,
        "full_history_analytics": False,
        "advanced_cost_analysis": False,
        "portfolio_risk": False,
        "prop_firm_cockpit": False,
        "hosted_mt5": False,
        "ai_coach_monthly_messages": 0,
    },
    "trader": {
        "max_accounts": 3,
        "full_history_analytics": True,
        "advanced_cost_analysis": True,
        "portfolio_risk": False,
        "prop_firm_cockpit": True,
        "hosted_mt5": False,
        "ai_coach_monthly_messages": 0,
    },
    "pro": {
        "max_accounts": 10,
        "full_history_analytics": True,
        "advanced_cost_analysis": True,
        "portfolio_risk": True,
        "prop_firm_cockpit": True,
        "hosted_mt5": True,
        "ai_coach_monthly_messages": 100,
    },
    "enterprise": {
        "max_accounts": None,
        "full_history_analytics": True,
        "advanced_cost_analysis": True,
        "portfolio_risk": True,
        "prop_firm_cockpit": True,
        "hosted_mt5": True,
        "ai_coach_monthly_messages": None,
    },
}


def get_user_entitlements(db: Client, user_id: str) -> dict[str, Any]:
    """Resolve entitlements only from server-owned subscription state."""
    response = (
        db.table("user_subscriptions")
        .select("plan,status,billing_period,current_period_end,cancel_at_period_end")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    row = response.data[0] if response.data else None
    paid = bool(row and row.get("status") in ("active", "trialing"))
    period_end = row.get("current_period_end") if row else None
    if paid and period_end:
        try:
            parsed_end = period_end if isinstance(period_end, datetime) else datetime.fromisoformat(str(period_end).replace("Z", "+00:00"))
            if parsed_end.tzinfo is None or parsed_end.utcoffset() is None:
                parsed_end = parsed_end.replace(tzinfo=timezone.utc)
            if parsed_end.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                paid = False
        except (ValueError, TypeError):
            # Malformed provider dates should never extend paid access indefinitely.
            paid = False
    plan = str(row.get("plan") or "free") if paid and row else "free"
    if plan not in PLAN_ENTITLEMENTS:
        plan = "free"
    return {
        "plan": plan,
        "subscription_status": row.get("status") if row else "inactive",
        "billing_period": row.get("billing_period") if paid and row else None,
        "current_period_end": row.get("current_period_end") if paid and row else None,
        "cancel_at_period_end": bool(row.get("cancel_at_period_end")) if paid and row else False,
        "paid": paid,
        "entitlements": dict(PLAN_ENTITLEMENTS[plan]),
    }


def require_account_feature(db: Client, account_id: str, feature: str) -> dict[str, Any]:
    """Enforce a plan entitlement at the backend boundary for an owned account.

    Callers must still use require_account_owner on the same endpoint first;
    this helper also checks existence so it can never infer ownership from a
    client-supplied account UUID alone.
    """
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
    allowed = entitlement["entitlements"].get(feature)
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
