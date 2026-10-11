from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from supabase import Client

from app.db import get_supabase
from app.deps import require_connector_token
from app.schemas import DealBatchIn, PositionSnapshotIn
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


@router.post("/positions")
def ingest_open_positions(
    batch: PositionSnapshotIn,
    account_id: str = Depends(require_connector_token),
    db: Client = Depends(get_supabase),
):
    """Commit a complete current-position snapshot for the token-scoped account.

    An incomplete new snapshot never replaces the last complete snapshot. The
    bridge supplies no credentials here; it authenticates with its scoped token.
    """
    account_result = (
        db.table("trading_accounts")
        .select("id,mt5_login,server,currency")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    if not account_result.data:
        raise HTTPException(status_code=404, detail="Trading account not found.")
    account = account_result.data[0]
    expected_login = str(account.get("mt5_login") or "").strip()
    incoming_login = str(batch.broker_login or "").strip()
    expected_server = str(account.get("server") or "").strip().casefold()
    incoming_server = str(batch.broker_server or "").strip().casefold()

    if expected_login and incoming_login and expected_login != incoming_login:
        raise HTTPException(status_code=409, detail="MT5 login does not match this Tradvoryx account profile.")
    if expected_server and incoming_server and expected_server != incoming_server:
        raise HTTPException(status_code=409, detail="MT5 server does not match this Tradvoryx account profile.")
    expected_currency = str(account.get("currency") or "").strip().upper()
    if expected_currency and expected_currency != batch.currency:
        raise HTTPException(
            status_code=409,
            detail=f"MT5 account currency {batch.currency} does not match the saved profile currency {expected_currency}. Update the profile before syncing.",
        )

    snapshot_at = batch.snapshot_at.isoformat()
    snapshot_insert = db.table("mt5_position_snapshots").insert({
        "account_id": account_id,
        "snapshot_at": snapshot_at,
        "currency": batch.currency,
        "balance": batch.balance,
        "equity": batch.equity,
        "is_complete": False,
    }).execute()
    if not snapshot_insert.data:
        raise HTTPException(status_code=500, detail="Could not start the MT5 position snapshot.")
    snapshot_id = snapshot_insert.data[0]["id"]

    rows = []
    for position in batch.positions:
        row = position.model_dump(mode="json")
        row.update({"snapshot_id": snapshot_id, "account_id": account_id})
        rows.append(row)

    try:
        for offset in range(0, len(rows), 1000):
            db.table("mt5_open_positions").insert(rows[offset:offset + 1000]).execute()
        committed = (
            db.table("mt5_position_snapshots")
            .update({"is_complete": True})
            .eq("id", snapshot_id)
            .eq("account_id", account_id)
            .execute()
        )
        if not committed.data:
            raise RuntimeError("Snapshot completion was not confirmed.")
    except Exception as exc:
        # Leave the new header incomplete; risk readers use only complete snapshots.
        raise HTTPException(
            status_code=502,
            detail="The current-position snapshot could not be committed. The last complete snapshot remains available.",
        ) from exc

    # This table is a current-state feed, not a tick archive. Once a replacement
    # is complete, remove older snapshots for this account (cascade removes items).
    db.table("mt5_position_snapshots").delete().eq("account_id", account_id).neq("id", snapshot_id).execute()
    db.table("trading_accounts").update({
        "connection_status": "connected",
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "current_balance": batch.balance,
        "current_equity": batch.equity,
    }).eq("id", account_id).execute()

    return {
        "snapshot_id": snapshot_id,
        "snapshot_at": snapshot_at,
        "positions_received": len(rows),
        "complete": True,
        "currency": batch.currency,
    }
