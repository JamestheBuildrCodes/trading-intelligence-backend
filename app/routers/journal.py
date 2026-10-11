from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from supabase import Client

from app.db import get_supabase
from app.deps import require_account_owner
from app.schemas import TradeJournalEntryIn, TradeJournalEntryOut

router = APIRouter(prefix="/api/v1/accounts", tags=["journal"])


@router.get("/{account_id}/journal", response_model=list[TradeJournalEntryOut])
def list_journal_entries(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    result = (
        db.table("trade_journal_entries")
        .select("*")
        .eq("account_id", account_id)
        .order("updated_at", desc=True)
        .limit(1000)
        .execute()
    )
    return result.data


@router.put("/{account_id}/journal/{trade_id}", response_model=TradeJournalEntryOut)
def save_journal_entry(
    body: TradeJournalEntryIn,
    trade_id: str,
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    # A valid account ID alone is insufficient: ensure the trade belongs to it.
    trade = (
        db.table("trades")
        .select("id")
        .eq("id", trade_id)
        .eq("account_id", account_id)
        .limit(1)
        .execute()
    )
    if not trade.data:
        raise HTTPException(status_code=404, detail="Trade not found for this account.")

    payload = body.model_dump()
    payload.update({
        "account_id": account_id,
        "trade_id": trade_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    result = (
        db.table("trade_journal_entries")
        .upsert(payload, on_conflict="account_id,trade_id")
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=500, detail="Journal entry could not be saved.")
    return result.data[0]
