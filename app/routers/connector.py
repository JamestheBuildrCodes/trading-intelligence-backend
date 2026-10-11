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
        # model_dump(mode='json') serializes timestamps for PostgREST.
        row = deal.model_dump(mode="json")
        row["account_id"] = account_id
        row["raw_payload"] = deal.model_dump(mode="json")
        db.table("raw_deals").upsert(
            row, on_conflict="account_id,mt5_deal_id", ignore_duplicates=True
        ).execute()
        position_ids.add(deal.mt5_position_id)

    for position_id in position_ids:
        aggregate_position(db, account_id, position_id)

    imported_cash_flows = 0
    for cash_flow in batch.cash_flows:
        flow_row = cash_flow.model_dump(mode="json")
        flow_row.update({"account_id": account_id, "source": "mt5"})
        db.table("portfolio_cash_flows").upsert(
            flow_row,
            on_conflict="account_id,external_id",
            ignore_duplicates=True,
        ).execute()
        imported_cash_flows += 1

    account_update = {
        "connection_status": "connected",
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
    }
    if batch.balance is not None:
        account_update["current_balance"] = batch.balance
    if batch.equity is not None:
        account_update["current_equity"] = batch.equity
    account_row_result = db.table("trading_accounts").select("currency").eq("id", account_id).limit(1).execute()
    account_row = (account_row_result.data or [{}])[0]
    db.table("trading_accounts").update(account_update).eq("id", account_id).execute()

    # Refresh today's snapshot on each sync; snapshots from prior days remain.
    if batch.balance is not None and batch.equity is not None:
        captured_at = datetime.now(timezone.utc)
        db.table("portfolio_snapshots").upsert(
            {
                "account_id": account_id,
                "snapshot_date": captured_at.date().isoformat(),
                "captured_at": captured_at.isoformat(),
                "balance": batch.balance,
                "equity": batch.equity,
                "currency": account_row.get("currency", "USD"),
                "source": "mt5_connector",
            },
            on_conflict="account_id,snapshot_date",
        ).execute()

    return {"received": len(batch.deals), "positions_recomputed": len(position_ids), "cash_flows_received": imported_cash_flows, "snapshot_recorded": batch.balance is not None and batch.equity is not None}
