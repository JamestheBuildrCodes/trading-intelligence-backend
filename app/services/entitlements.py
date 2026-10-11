from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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
