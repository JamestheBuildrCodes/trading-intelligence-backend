"""Read-only TickerAll history sync for Tradvoryx.

Only GET requests are used for account data. This worker never places, modifies,
or closes broker orders. It stores closed round-trips and balance operations.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from supabase import Client

from app.config import get_settings
from app.db import get_supabase
from app.services.aggregation import classify_session

BASE_URL = "https://api.tickerall.com"
CHUNK_DAYS = 180
INITIAL_LOOKBACK_DAYS = max(365, int(os.getenv("TICKERALL_INITIAL_LOOKBACK_DAYS", "10000")))
log = logging.getLogger("tradvoryx.tickerall_sync")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")


def parse_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("provider_time_missing")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def get_json(client: httpx.AsyncClient, token: str, path: str, params: dict | None = None) -> dict:
    response = await client.get(
        BASE_URL + path,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    if response.status_code >= 400:
        # Never print raw provider messages or credentials.
        code = "tickerall_auth_failed" if response.status_code == 401 else (
            "tickerall_account_not_hot" if response.status_code == 409 else "tickerall_read_failed"
        )
        raise RuntimeError(code)
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("tickerall_invalid_response")
    return payload


async def fetch_history_range(client: httpx.AsyncClient, token: str, provider_id: str,
                              start: datetime, end: datetime, depth: int = 0) -> list[dict]:
    if end <= start:
        return []
    payload = await get_json(
        client, token, f"/v1/accounts/{provider_id}/history",
        {"from": start.isoformat(), "to": end.isoformat(), "limit": 5000, "waitMs": 0},
    )
    trades = payload.get("trades")
    if not isinstance(trades, list):
        raise RuntimeError("tickerall_history_shape_invalid")
    if len(trades) < 5000:
        return [row for row in trades if isinstance(row, dict)]
    # Never advance a cursor past a possibly truncated provider response.
    if depth >= 16 or (end - start).total_seconds() <= 1:
        raise RuntimeError("history_response_limit_reached")
    middle = start + (end - start) / 2
    left = await fetch_history_range(client, token, provider_id, start, middle, depth + 1)
    right = await fetch_history_range(client, token, provider_id, middle, end, depth + 1)
    by_key: dict[str, dict] = {}
    for row in left + right:
        key = str(row.get("closeTicket") or row.get("ticket") or "")
        if key:
            by_key[key] = row
    return sorted(by_key.values(), key=lambda row: parse_time(row.get("closeTime")))


async def fetch_balance_range(client: httpx.AsyncClient, token: str, provider_id: str,
                              start: datetime, end: datetime, depth: int = 0) -> list[dict]:
    if end <= start:
        return []
    payload = await get_json(
        client, token, f"/v1/accounts/{provider_id}/balance-operations",
        {"from": start.isoformat(), "to": end.isoformat(), "limit": 5000, "waitMs": 0},
    )
    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise RuntimeError("tickerall_balance_history_shape_invalid")
    if len(operations) < 5000:
        return [row for row in operations if isinstance(row, dict)]
    if depth >= 16 or (end - start).total_seconds() <= 1:
        raise RuntimeError("history_response_limit_reached")
    middle = start + (end - start) / 2
    left = await fetch_balance_range(client, token, provider_id, start, middle, depth + 1)
    right = await fetch_balance_range(client, token, provider_id, middle, end, depth + 1)
    by_key: dict[str, dict] = {}
    for row in left + right:
        key = str(row.get("ticket") or "")
        if key:
            by_key[key] = row
    return sorted(by_key.values(), key=lambda row: parse_time(row.get("time")))


def normalize_trade(row: dict, account_id: str) -> dict | None:
    try:
        ticket = str(row.get("ticket") or "").strip()
        close_ticket = str(row.get("closeTicket") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        side = str(row.get("side") or "").lower()
        volume = float(row.get("volume") or 0)
        entry = float(row.get("openPrice") or 0)
        exit_price = float(row.get("closePrice") or 0)
        profit = float(row.get("profit") or 0)
        swap = float(row.get("swap") or 0)
        commission = float(row.get("commission") or 0)
        opened = parse_time(row.get("openTime"))
        closed = parse_time(row.get("closeTime"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not ticket or not close_ticket or not symbol or side not in {"buy", "sell"} or volume <= 0 or closed < opened:
        return None
    # TickerAll returns closed round-trips rather than the MT5 raw deal ledger.
    # The composite key preserves separate partial-close rows without collisions.
    position_key = f"tickerall:{ticket}:{close_ticket}"
    return {
        "account_id": account_id,
        "mt5_position_id": position_key,
        "symbol": symbol,
        "direction": side,
        "volume": volume,
        "entry_price": entry,
        "exit_price": exit_price,
        "entry_at": opened.isoformat(),
        "exit_at": closed.isoformat(),
        "holding_seconds": max(0, int((closed - opened).total_seconds())),
        "is_partial_close": False,
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "session": classify_session(opened),
        "day_of_week": opened.isoweekday() % 7,
    }


def write_cash_flows(db: Client, account_id: str, operations: list[dict]) -> int:
    rows = []
    for item in operations:
        kind = str(item.get("type") or "").lower()
        if kind not in {"deposit", "withdrawal"}:
            continue
        try:
            amount = abs(float(item.get("amount") or 0))
            occurred = parse_time(item.get("time"))
            external_id = str(item.get("ticket") or "").strip()
        except (TypeError, ValueError, OverflowError):
            continue
        if amount <= 0 or not external_id:
            continue
        rows.append({
            "account_id": account_id,
            "external_id": external_id,
            "flow_type": "deposit" if kind == "deposit" else "withdrawal",
            "amount": amount,
            "occurred_at": occurred.isoformat(),
            "note": "Imported from TickerAll balance history",
            "source": "mt5",
        })
    for offset in range(0, len(rows), 500):
        db.table("portfolio_cash_flows").upsert(
            rows[offset:offset + 500],
            on_conflict="account_id,external_id",
            ignore_duplicates=True,
        ).execute()
    return len(rows)


def save_snapshot(db: Client, account_id: str, saved: dict, remote: dict, captured: datetime) -> int:
    info = remote.get("account") if isinstance(remote.get("account"), dict) else {}
    currency = str(info.get("currency") or saved.get("currency") or "").upper()
    if not currency:
        raise RuntimeError("account_identity_mismatch")
    if saved.get("currency") and str(saved["currency"]).upper() != currency:
        raise RuntimeError("account_identity_mismatch")
    balance = float(info.get("balance") or 0)
    equity = float(info.get("equity") if info.get("equity") is not None else balance)
    positions = remote.get("positions") if isinstance(remote.get("positions"), list) else []
    header = db.table("mt5_position_snapshots").insert({
        "account_id": account_id, "snapshot_at": captured.isoformat(),
        "currency": currency, "balance": balance, "equity": equity, "is_complete": False,
    }).execute()
    if not header.data:
        raise RuntimeError("database_write_failed")
    snapshot_id = header.data[0]["id"]
    mapped = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        side = str(p.get("side") or "").lower()
        symbol = str(p.get("symbol") or "").strip()
        ticket = str(p.get("ticket") or "").strip()
        try:
            volume = float(p.get("volume") or 0)
            open_price = float(p.get("entryPrice") or 0)
            current_price = float(p.get("currentPrice") or 0)
            opened = parse_time(p.get("openTime"))
            profit = float(p.get("profit") or 0)
            swap = float(p.get("swap") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if not ticket or not symbol or side not in {"buy", "sell"} or volume <= 0:
            continue
        sl = p.get("stopLoss")
        tp = p.get("takeProfit")
        mapped.append({
            "snapshot_id": snapshot_id, "account_id": account_id,
            "mt5_position_id": ticket, "symbol": symbol, "direction": side,
            "volume": volume, "open_price": open_price, "current_price": current_price,
            "stop_loss": float(sl) if sl not in (None, 0, "0") else None,
            "take_profit": float(tp) if tp not in (None, 0, "0") else None,
            "unrealized_profit": profit, "swap": swap, "risk_to_stop": None,
            "opened_at": opened.isoformat(),
        })
    try:
        for offset in range(0, len(mapped), 500):
            db.table("mt5_open_positions").insert(mapped[offset:offset + 500]).execute()
        committed = db.table("mt5_position_snapshots").update({"is_complete": True}).eq("id", snapshot_id).eq("account_id", account_id).execute()
        if not committed.data:
            raise RuntimeError("database_write_failed")
    except Exception:
        raise RuntimeError("database_write_failed") from None
    db.table("mt5_position_snapshots").delete().eq("account_id", account_id).neq("id", snapshot_id).execute()
    db.table("trading_accounts").update({
        "connection_status": "connected", "last_sync_at": captured.isoformat(),
        "current_balance": balance, "current_equity": equity,
        "hosted_connection_status": "connected", "hosted_last_sync_at": captured.isoformat(),
        "hosted_connection_verified_at": captured.isoformat(), "hosted_last_error_code": None,
        "hosted_status_updated_at": captured.isoformat(),
    }).eq("id", account_id).execute()
    db.table("portfolio_snapshots").upsert({
        "account_id": account_id, "snapshot_date": captured.date().isoformat(),
        "captured_at": captured.isoformat(), "balance": balance, "equity": equity,
        "currency": currency, "source": "mt5_connector",
    }, on_conflict="account_id,snapshot_date").execute()
    return len(mapped)


async def sync_one(db: Client, account_id: str, max_chunks: int) -> None:
    settings = get_settings()
    token = settings.tickerall_api_key
    if not token:
        raise RuntimeError("missing_required_configuration:TICKERALL_API_KEY")
    result = db.table("trading_accounts").select("*").eq("id", account_id).limit(1).execute()
    if not result.data:
        raise RuntimeError("target_account_not_found")
    saved = result.data[0]
    provider_id = str(saved.get("provider_account_id") or "")
    if saved.get("connection_provider") != "tickerall" or not provider_id:
        raise RuntimeError("target_account_not_tickerall")
    started = datetime.now(timezone.utc)
    run_result = db.table("hosted_mt5_worker_runs").insert({
        "account_id": account_id, "started_at": started.isoformat(), "status": "running",
    }).execute()
    if not run_result.data:
        raise RuntimeError("worker_run_health_record_could_not_be_created")
    run_id = run_result.data[0]["id"]
    status = "failed"
    safe_error = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0)) as client:
            remote = await get_json(client, token, f"/v1/accounts/{provider_id}")
            if str(remote.get("status") or "").lower() not in {"online", "connected"}:
                raise RuntimeError("tickerall_account_not_hot")
            info = remote.get("account") if isinstance(remote.get("account"), dict) else {}
            remote_login = str(remote.get("accountNumber") or "").replace("*", "").strip()
            saved_login = str(saved.get("mt5_login") or "").strip()
            # TickerAll masks account numbers; compare only when it returns an unmasked value.
            if remote_login and not remote_login.startswith("*") and saved_login and remote_login != saved_login:
                raise RuntimeError("account_identity_mismatch")
            currency = str(info.get("currency") or "").upper()
            if saved.get("currency") and currency and str(saved["currency"]).upper() != currency:
                raise RuntimeError("account_identity_mismatch")

            complete = bool(saved.get("hosted_initial_backfill_complete"))
            cursor_raw = saved.get("hosted_history_cursor_at")
            cursor = parse_time(cursor_raw) if cursor_raw else started - timedelta(days=INITIAL_LOOKBACK_DAYS)
            if complete:
                cursor = max(cursor - timedelta(days=2), started - timedelta(days=2))
            chunks_done = 0
            all_rows: list[dict] = []
            all_operations: list[dict] = []
            first_seen = parse_time(saved["hosted_first_deal_at"]) if saved.get("hosted_first_deal_at") else None
            last_seen = parse_time(saved["hosted_last_deal_at"]) if saved.get("hosted_last_deal_at") else None
            while cursor < started and chunks_done < max_chunks:
                end = min(cursor + timedelta(days=CHUNK_DAYS), started)
                history = await fetch_history_range(client, token, provider_id, cursor, end)
                operations = await fetch_balance_range(client, token, provider_id, cursor, end)
                mapped = [normalize_trade(item, account_id) for item in history]
                mapped = [item for item in mapped if item is not None]
                if mapped:
                    for offset in range(0, len(mapped), 500):
                        db.table("trades").upsert(mapped[offset:offset + 500], on_conflict="account_id,mt5_position_id").execute()
                    observed = [parse_time(item["exit_at"]) for item in mapped]
                    first_seen = min([v for v in [first_seen, *observed] if v is not None])
                    last_seen = max([v for v in [last_seen, *observed] if v is not None])
                    all_rows.extend(mapped)
                all_operations.extend(operations)
                cursor = end
                chunks_done += 1
                db.table("trading_accounts").update({
                    "hosted_history_cursor_at": cursor.isoformat(),
                    "hosted_first_deal_at": first_seen.isoformat() if first_seen else None,
                    "hosted_last_deal_at": last_seen.isoformat() if last_seen else None,
                    "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", account_id).execute()
            write_cash_flows(db, account_id, all_operations)
            positions = save_snapshot(db, account_id, saved, remote, datetime.now(timezone.utc))
            complete = cursor >= started
            status = "succeeded" if complete else "partial"
            safe_error = None if complete else "backfill_incomplete_more_runs_required"
            db.table("trading_accounts").update({
                "hosted_initial_backfill_complete": complete,
                "hosted_history_cursor_at": cursor.isoformat(),
                "hosted_first_deal_at": first_seen.isoformat() if first_seen else None,
                "hosted_last_deal_at": last_seen.isoformat() if last_seen else None,
                "hosted_last_sync_at": datetime.now(timezone.utc).isoformat(),
                "hosted_connection_status": "connected",
                "hosted_last_error_code": safe_error,
            }).eq("id", account_id).execute()
            log.info("TickerAll sync completed: account_ref=...%s trades_upserted=%d positions=%d chunks=%d backfill_complete=%s",
                     account_id[-8:], len(all_rows), positions, chunks_done, complete)
    except Exception as exc:
        candidate = str(exc)
        safe_codes = {
            "missing_required_configuration:TICKERALL_API_KEY", "target_account_not_found",
            "target_account_not_tickerall", "tickerall_auth_failed", "tickerall_account_not_hot",
            "tickerall_read_failed", "tickerall_invalid_response", "tickerall_history_shape_invalid",
            "tickerall_balance_history_shape_invalid", "history_response_limit_reached",
            "account_identity_mismatch", "database_write_failed",
        }
        safe_error = candidate if candidate in safe_codes else "tickerall_sync_failed"
        db.table("trading_accounts").update({
            "hosted_connection_status": "error", "hosted_last_error_code": safe_error,
            "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", account_id).execute()
        log.warning("TickerAll sync failed safely: account_ref=...%s error_code=%s", account_id[-8:], safe_error)
        raise RuntimeError(safe_error) from None
    finally:
        db.table("hosted_mt5_worker_runs").update({
            "completed_at": datetime.now(timezone.utc).isoformat(), "status": status,
            "accounts_seen": 1, "accounts_succeeded": 1 if status in {"succeeded", "partial"} else 0,
            "accounts_failed": 0 if status in {"succeeded", "partial"} else 1,
            "safe_error_code": safe_error,
        }).eq("id", run_id).execute()


async def run_once(account_id: str, max_chunks: int) -> None:
    db = get_supabase()
    await sync_one(db, account_id, max_chunks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only TickerAll hosted MT5 history sync.")
    parser.add_argument("--account-id", required=True, help="Exact Tradvoryx trading_accounts UUID.")
    parser.add_argument("--max-history-chunks", type=int, default=2, choices=range(1, 21))
    args = parser.parse_args()
    try:
        asyncio.run(run_once(args.account_id, args.max_history_chunks))
    except Exception as exc:
        candidate = str(exc)
        safe = candidate if candidate.startswith("missing_required_configuration:") or candidate in {
            "target_account_not_found", "target_account_not_tickerall", "tickerall_auth_failed",
            "tickerall_account_not_hot", "tickerall_read_failed", "tickerall_invalid_response",
            "tickerall_history_shape_invalid", "tickerall_balance_history_shape_invalid",
            "history_response_limit_reached", "account_identity_mismatch", "database_write_failed",
            "worker_run_health_record_could_not_be_created", "tickerall_sync_failed",
            "backfill_incomplete_more_runs_required",
        } else "tickerall_sync_failed"
        log.error("TickerAll sync failed: error_code=%s", safe)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
