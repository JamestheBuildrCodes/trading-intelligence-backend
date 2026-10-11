from fastapi import APIRouter, Depends, Query
from supabase import Client

from app.db import get_supabase
from app.deps import require_account_owner
from app.schemas import OverviewOut, TradeOut
from app.services.analytics import compute_cost_analysis, compute_overview, compute_performance_breakdown, compute_trading_dna

router = APIRouter(prefix="/api/v1/accounts", tags=["overview"])


@router.get("/{account_id}/overview", response_model=OverviewOut)
def get_overview(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    return compute_overview(db, account_id)


@router.get("/{account_id}/trades", response_model=list[TradeOut])
def list_trades(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
    symbol: str | None = Query(default=None),
    session: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    query = (
        db.table("trades")
        .select("*")
        .eq("account_id", account_id)
        .not_.is_("exit_at", "null")
        .order("exit_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if symbol:
        query = query.eq("symbol", symbol)
    if session:
        query = query.eq("session", session)

    return query.execute().data



@router.get("/{account_id}/performance-breakdown")
def get_performance_breakdown(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    """Return grouped insights calculated over every imported closed trade."""
    return compute_performance_breakdown(db, account_id)


@router.get("/{account_id}/cost-analysis")
def get_cost_analysis(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    """Return cost and self-reported execution-review metrics over all imported history."""
    return compute_cost_analysis(db, account_id)


@router.get("/{account_id}/trading-dna")
def get_trading_dna(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    return compute_trading_dna(db, account_id)
