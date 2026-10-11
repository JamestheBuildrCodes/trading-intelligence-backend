"""Scheduled hosted MT5 synchronizer for MetaApi cloud accounts.

Run as a Render Cron Job after setting the required secrets:
  python -m app.workers.hosted_mt5_sync

The job performs read-only RPC operations only. Broker passwords are handled by
MetaApi at account provisioning time; TradeGrid never stores them.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from metaapi_cloud_sdk import MetaApi
from supabase import Client

from app.config import get_settings
from app.db import get_supabase
from app.services.aggregation import aggregate_position
from app.services.metaapi_adapter import fetch_all_deals_for_range, map_metaapi_deal, map_metaapi_position, parse_provider_time

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradegrid.hosted_mt5")

INITIAL_LOOKBACK_DAYS = max(365, int(os.getenv("METAAPI_INITIAL_LOOKBACK_DAYS", "10000")))
HISTORY_CHUNK_DAYS = max(1, int(os.getenv("METAAPI_HISTORY_CHUNK_DAYS", "180")))
MAX_HISTORY_CHUNKS_PER_RUN = max(1, int(os.getenv("METAAPI_MAX_HISTORY_CHUNKS_PER_RUN", "10")))
INCREMENTAL_OVERLAP_DAYS = max(1, int(os.getenv("METAAPI_SYNC_OVERLAP_DAYS", "2")))
POSITION_SNAPSHOT_STALE_SECONDS = max(60, int(os.getenv("METAAPI_POSITION_SNAPSHOT_MAX_AGE_SECONDS", "300")))


def _time(value: Any) -> datetime | None:
    try:
        return parse_provider_time(value) if value else None
    except (ValueError, TypeError, OverflowError):
        return None


def _currency(value: Any) -> str:
    return str(value or "").strip().upper()


def _safe_status_error(db: Client, account_id: str, code: str) -> None:
    # Fixed allow-listed codes only; never persist arbitrary provider exception text.
    allowed = {
        "provider_account_lookup_failed",
        "provider_connection_failed",
        "account_identity_mismatch",
        "history_sync_failed",
        "history_response_limit_reached",
        "position_snapshot_failed",
        "database_write_failed",
    }
    db.table("trading_accounts").update({
        "hosted_connection_status": "error",
        "hosted_last_error_code": code if code in allowed else "provider_connection_failed",
        "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", account_id).execute()
    db.table("trading_accounts").update({"connection_status": "error"}).eq("id", account_id).execute()


def _latest_position_snapshot(db: Client, account_id: str) -> dict | None:
    result = (
        db.table("mt5_position_snapshots")
        .select("id,snapshot_at,currency,balance,equity,is_complete")
        .eq("account_id", account_id)
        .eq("is_complete", True)
        .order("snapshot_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _upload_cash_flows(db: Client, account_id: str, cash_flows: list[dict[str, Any]]) -> None:
    for offset in range(0, len(cash_flows), 1000):
        rows = []
        for cash_flow in cash_flows[offset:offset + 1000]:
            row = dict(cash_flow)
            row.update({"account_id": account_id, "source": "mt5"})
            rows.append(row)
        if rows:
            db.table("portfolio_cash_flows").upsert(
                rows, on_conflict="account_id,external_id", ignore_duplicates=True
            ).execute()


def _upload_deals(db: Client, account_id: str, deals: list[dict[str, Any]]) -> int:
    position_ids: set[str] = set()
    for offset in range(0, len(deals), 1000):
        batch = []
        for deal in deals[offset:offset + 1000]:
            row = dict(deal)
            row["account_id"] = account_id
            row["raw_payload"] = dict(deal)
            batch.append(row)
            position_ids.add(str(deal["mt5_position_id"]))
        if batch:
            db.table("raw_deals").upsert(
                batch, on_conflict="account_id,mt5_deal_id", ignore_duplicates=True
            ).execute()
    for position_id in position_ids:
        aggregate_position(db, account_id, position_id)
    return len(position_ids)


def _write_position_snapshot(
    db: Client, account: dict, info: dict, remote_positions: list[dict], captured_at: datetime
) -> int:
    account_id = str(account["id"])
    currency = _currency(info.get("currency"))
    saved_currency = _currency(account.get("currency"))
    remote_login = str(info.get("login") or "").strip()
    remote_server = str(info.get("server") or "").strip()
    saved_login = str(account.get("mt5_login") or "").strip()
    saved_server = str(account.get("server") or "").strip()

    if saved_login and remote_login and saved_login != remote_login:
        raise ValueError("account_identity_mismatch")
    if saved_server and remote_server and saved_server.casefold() != remote_server.casefold():
        raise ValueError("account_identity_mismatch")
    if saved_currency and currency and saved_currency != currency:
        raise ValueError("account_identity_mismatch")
    if not currency:
        raise ValueError("account_identity_mismatch")

    snapshot_result = db.table("mt5_position_snapshots").insert({
        "account_id": account_id,
        "snapshot_at": captured_at.isoformat(),
        "currency": currency,
        "balance": float(info.get("balance") or 0),
        "equity": float(info.get("equity") or 0),
        "is_complete": False,
    }).execute()
    if not snapshot_result.data:
        raise RuntimeError("database_write_failed")
    snapshot_id = snapshot_result.data[0]["id"]

    normalized_positions = []
    for position in remote_positions or []:
        mapped = map_metaapi_position(position)
        if mapped is not None:
            mapped.update({"snapshot_id": snapshot_id, "account_id": account_id})
            normalized_positions.append(mapped)

    try:
        for offset in range(0, len(normalized_positions), 1000):
            db.table("mt5_open_positions").insert(normalized_positions[offset:offset + 1000]).execute()
        marked = (
            db.table("mt5_position_snapshots")
            .update({"is_complete": True})
            .eq("id", snapshot_id)
            .eq("account_id", account_id)
            .execute()
        )
        if not marked.data:
            raise RuntimeError("database_write_failed")
    except Exception:
        # Incomplete snapshot remains excluded; previous complete state is preserved.
        raise

    # Keep only the current-state snapshot for this account.
    db.table("mt5_position_snapshots").delete().eq("account_id", account_id).neq("id", snapshot_id).execute()
    db.table("trading_accounts").update({
        "connection_status": "connected",
        "last_sync_at": captured_at.isoformat(),
        "current_balance": float(info.get("balance") or 0),
        "current_equity": float(info.get("equity") or 0),
    }).eq("id", account_id).execute()
    db.table("portfolio_snapshots").upsert({
        "account_id": account_id,
        "snapshot_date": captured_at.date().isoformat(),
        "captured_at": captured_at.isoformat(),
        "balance": float(info.get("balance") or 0),
        "equity": float(info.get("equity") or 0),
        "currency": currency,
        "source": "mt5_connector",
    }, on_conflict="account_id,snapshot_date").execute()
    return len(normalized_positions)


async def _sync_one(api: MetaApi, db: Client, row: dict, now: datetime) -> None:
    account_id = str(row["id"])
    provider_id = str(row.get("provider_account_id") or "")
    if not provider_id:
        _safe_status_error(db, account_id, "provider_account_lookup_failed")
        return

    remote = None
    connection = None
    try:
        remote = await api.metatrader_account_api.get_account(provider_id)
        provider_state = str(getattr(remote, "state", "") or "").upper()
        if not any(state in provider_state for state in ("DEPLOYED", "CONNECTED", "DEPLOYING")):
            await remote.deploy()
        db.table("trading_accounts").update({
            "hosted_connection_status": "connecting",
            "hosted_last_error_code": None,
            "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", account_id).execute()
        await remote.wait_connected()

        connection = remote.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()

        info = await connection.get_account_information()
        if not isinstance(info, dict):
            raise ValueError("account_identity_mismatch")
        saved_login = str(row.get("mt5_login") or "").strip()
        incoming_login = str(info.get("login") or "").strip()
        saved_server = str(row.get("server") or "").strip()
        incoming_server = str(info.get("server") or "").strip()
        if (
            (saved_login and incoming_login and saved_login != incoming_login)
            or (saved_server and incoming_server and saved_server.casefold() != incoming_server.casefold())
            or (_currency(row.get("currency")) and _currency(info.get("currency")) != _currency(row.get("currency")))
        ):
            raise ValueError("account_identity_mismatch")

        db.table("trading_accounts").update({
            "hosted_connection_status": "syncing",
            "hosted_connection_verified_at": datetime.now(timezone.utc).isoformat(),
            "hosted_last_error_code": None,
            "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", account_id).execute()

        complete = bool(row.get("hosted_initial_backfill_complete"))
        raw_cursor = row.get("hosted_history_cursor_at")
        cursor = _time(raw_cursor) if raw_cursor else None
        if cursor is None:
            cursor = now - timedelta(days=INITIAL_LOOKBACK_DAYS)
        elif complete:
            cursor = max(cursor - timedelta(days=INCREMENTAL_OVERLAP_DAYS), now - timedelta(days=HISTORY_CHUNK_DAYS))

        first_deal = _time(row.get("hosted_first_deal_at"))
        last_deal = _time(row.get("hosted_last_deal_at"))
        chunks_done = 0
        deals_uploaded_total = 0

        while cursor < now and chunks_done < MAX_HISTORY_CHUNKS_PER_RUN:
            window_end = min(cursor + timedelta(days=HISTORY_CHUNK_DAYS), now)
            # SDK examples use naive UTC datetimes for time-range RPC calls.
            provider_start = cursor.astimezone(timezone.utc).replace(tzinfo=None)
            provider_end = window_end.astimezone(timezone.utc).replace(tzinfo=None)
            fetched = await fetch_all_deals_for_range(connection, provider_start, provider_end)
            if fetched is None:
                raise RuntimeError("history_sync_failed")

            directions: dict[str, str] = {}
            ordered = sorted(
                fetched,
                key=lambda deal: (
                    _time(deal.get("time") or deal.get("dealTime") if isinstance(deal, dict) else None)
                    or datetime.min.replace(tzinfo=timezone.utc),
                    str(deal.get("id") or deal.get("ticket") or "") if isinstance(deal, dict) else "",
                ),
            )
            normalized_deals: list[dict[str, Any]] = []
            cash_flows: list[dict[str, Any]] = []
            observed_times: list[datetime] = []
            for deal in ordered:
                if not isinstance(deal, dict):
                    continue
                deal_time = _time(deal.get("time") or deal.get("dealTime"))
                if deal_time:
                    observed_times.append(deal_time)
                normalized, flow = map_metaapi_deal(deal, directions)
                if normalized is not None:
                    normalized_deals.append(normalized)
                if flow is not None:
                    cash_flows.append(flow)
            _upload_cash_flows(db, account_id, cash_flows)
            _upload_deals(db, account_id, normalized_deals)
            deals_uploaded_total += len(normalized_deals)
            if observed_times:
                first_observed = min(observed_times)
                last_observed = max(observed_times)
                first_deal = min(filter(None, [first_deal, first_observed]))
                last_deal = max(filter(None, [last_deal, last_observed]))

            # Cursor advances only after all writes for the window have succeeded.
            cursor = window_end
            chunks_done += 1
            current_now = datetime.now(timezone.utc)
            complete = cursor >= now
            db.table("trading_accounts").update({
                "hosted_history_cursor_at": cursor.isoformat(),
                "hosted_initial_backfill_complete": complete,
                "hosted_first_deal_at": first_deal.isoformat() if first_deal else None,
                "hosted_last_deal_at": last_deal.isoformat() if last_deal else None,
                "hosted_last_sync_at": current_now.isoformat(),
                "hosted_connection_status": "syncing" if not complete else "connected",
                "hosted_last_error_code": None,
                "hosted_status_updated_at": current_now.isoformat(),
            }).eq("id", account_id).execute()

        # Position sync always runs, even when this job is only part-way through backfill.
        positions = await connection.get_positions()
        _write_position_snapshot(db, row, info, positions or [], datetime.now(timezone.utc))
        final_status = "connected" if complete else "syncing"
        done = datetime.now(timezone.utc)
        db.table("trading_accounts").update({
            "hosted_connection_status": final_status,
            "hosted_last_sync_at": done.isoformat(),
            "hosted_last_error_code": None,
            "hosted_status_updated_at": done.isoformat(),
        }).eq("id", account_id).execute()
        log.info(
            "Hosted MT5 sync account_id=%s status=%s chunks=%s normalized_deals=%s positions=%s backfill_complete=%s",
            account_id, final_status, chunks_done, deals_uploaded_total, len(positions or []), complete,
        )
    except Exception as exc:
        exc_code = str(exc) if str(exc) in {
            "account_identity_mismatch", "history_sync_failed", "history_response_limit_reached", "position_snapshot_failed", "database_write_failed"
        } else "provider_connection_failed"
        _safe_status_error(db, account_id, exc_code)
        # Never log arbitrary SDK exception text; it may carry provider/request details.
        log.warning("Hosted MT5 sync failed for account_id=%s error_code=%s", account_id, exc_code)
    finally:
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass


async def run_once() -> None:
    settings = get_settings()
    if not settings.metaapi_token:
        log.error("Hosted MT5 worker is not configured: METAAPI_TOKEN is missing.")
        return

    db = get_supabase()
    api = MetaApi(settings.metaapi_token)
    rows = (
        db.table("trading_accounts")
        .select("*")
        .eq("connection_provider", "metaapi")
        .neq("hosted_connection_status", "disconnected")
        .execute()
    ).data or []
    if not rows:
        log.info("No hosted MT5 accounts are queued.")
        return
    now = datetime.now(timezone.utc)
    for row in rows:
        await _sync_one(api, db, row, now)


def main() -> None:
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
