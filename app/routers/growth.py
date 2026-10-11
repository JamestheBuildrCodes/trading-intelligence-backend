from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from supabase import Client

from app.db import get_supabase
from app.deps import CurrentUser, get_current_user, require_account_owner
from app.schemas import CashFlowIn, CashFlowOut, PortfolioGrowthOut

router = APIRouter(prefix="/api/v1/accounts", tags=["portfolio-growth"])


def _parse_time(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@router.get("/{account_id}/growth", response_model=PortfolioGrowthOut)
def get_portfolio_growth(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    snapshots = (
        db.table("portfolio_snapshots").select("*").eq("account_id", account_id)
        .order("snapshot_date").limit(2000).execute().data or []
    )
    flows = (
        db.table("portfolio_cash_flows").select("*").eq("account_id", account_id)
        .order("occurred_at").limit(2000).execute().data or []
    )
    account_result = db.table("trading_accounts").select("currency").eq("id", account_id).limit(1).execute()
    currency = (account_result.data or [{}])[0].get("currency", "USD")

    if not snapshots:
        return {
            "currency": currency, "tracking_started_at": None, "latest_snapshot_at": None,
            "starting_equity": None, "latest_equity": None,
            "net_external_cash_flow": sum(float(row["amount"]) * (1 if row["flow_type"] == "deposit" else -1) for row in flows),
            "return_pct": None, "snapshots": [], "cash_flows": flows,
        }

    first, latest = snapshots[0], snapshots[-1]
    start_time, end_time = _parse_time(first["captured_at"]), _parse_time(latest["captured_at"])
    start_equity, end_equity = float(first["equity"]), float(latest["equity"])
    duration = (end_time - start_time).total_seconds()
    in_period = []
    for flow in flows:
        flow_time = _parse_time(flow["occurred_at"])
        if start_time < flow_time <= end_time:
            amount = float(flow["amount"]) * (1 if flow["flow_type"] == "deposit" else -1)
            in_period.append((flow_time, amount))
    net_flow = sum(amount for _, amount in in_period)
    weighted_flow = 0.0
    if duration > 0:
        for flow_time, amount in in_period:
            weight = max(0.0, min(1.0, (end_time - flow_time).total_seconds() / duration))
            weighted_flow += amount * weight
    denominator = start_equity + weighted_flow
    return_pct = ((end_equity - start_equity - net_flow) / denominator * 100.0) if duration > 0 and denominator > 0 else None

    return {
        "currency": currency, "tracking_started_at": first["captured_at"],
        "latest_snapshot_at": latest["captured_at"], "starting_equity": start_equity,
        "latest_equity": end_equity, "net_external_cash_flow": net_flow,
        "return_pct": return_pct, "snapshots": snapshots, "cash_flows": flows,
    }


@router.post("/{account_id}/cash-flows", response_model=CashFlowOut, status_code=201)
def create_cash_flow(
    body: CashFlowIn,
    account_id: str = Depends(require_account_owner),
    user: CurrentUser = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    row = body.model_dump(mode="json")
    row.update({"account_id": account_id, "created_by": user.id})
    result = db.table("portfolio_cash_flows").insert(row).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Cash flow could not be recorded.")
    return result.data[0]
