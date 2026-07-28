from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from supabase import Client

from app.db import get_supabase
from app.deps import require_connector_token
from app.schemas import DealBatchIn
from app.services.aggregation import aggregate_position

router = APIRouter(prefix="/api/v1/connector", tags=["connector"])


@router.post("/deals")
def ingest_deals(
    batch: DealBatchIn,
    account_id: str = Depends(require_connector_token),
    db: Client = Depends(get_supabase),
):
    position_ids: set[str] = set()

    for deal in batch.deals:
        row = deal.model_dump()
        row["account_id"] = account_id
        row["deal_time"] = deal.deal_time.isoformat()
        row["raw_payload"] = deal.model_dump(mode="json")
        # ignore_duplicates makes this safe to retry — a resent deal is a no-op.
        db.table("raw_deals").upsert(
            row, on_conflict="account_id,mt5_deal_id", ignore_duplicates=True
        ).execute()
        position_ids.add(deal.mt5_position_id)

    for position_id in position_ids:
        aggregate_position(db, account_id, position_id)

    account_update = {
        "connection_status": "connected",
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
    }
    if batch.balance is not None:
        account_update["current_balance"] = batch.balance
    if batch.equity is not None:
        account_update["current_equity"] = batch.equity

    db.table("trading_accounts").update(account_update).eq("id", account_id).execute()

    return {"ingested": len(batch.deals), "positions_recomputed": len(position_ids)}
