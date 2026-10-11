"""Read-only MetaTrader 5 -> TradeGrid connector.

Runs on Windows beside a logged-in MetaTrader 5 terminal. It never places,
modifies, or closes orders. Deal uploads are idempotent on the backend.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tradegrid.mt5_bridge")

API_BASE = os.getenv(
    "TRADEGRID_API_BASE_URL",
    "https://trading-intelligence-backend.onrender.com",
).rstrip("/")
ACCOUNT_ID = os.getenv("TRADEGRID_ACCOUNT_ID", "").strip()
CONNECTOR_TOKEN = os.getenv("TRADEGRID_CONNECTOR_TOKEN", "").strip()
TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH", "").strip() or None
INITIAL_LOOKBACK_DAYS = max(1, int(os.getenv("MT5_INITIAL_LOOKBACK_DAYS", "10000")))
HISTORY_CHUNK_DAYS = max(1, int(os.getenv("MT5_HISTORY_CHUNK_DAYS", "90")))
OVERLAP_DAYS = max(1, int(os.getenv("MT5_SYNC_OVERLAP_DAYS", "2")))
POLL_SECONDS = max(15, int(os.getenv("MT5_POLL_SECONDS", "60")))
BATCH_SIZE = min(500, max(1, int(os.getenv("MT5_BATCH_SIZE", "250"))))
SYNC_STATE_PATH = Path(os.getenv("MT5_SYNC_STATE_PATH", ".tradegrid_sync_state.json"))


def require_config() -> None:
    missing = [
        name for name, value in (
            ("TRADEGRID_ACCOUNT_ID", ACCOUNT_ID),
            ("TRADEGRID_CONNECTOR_TOKEN", CONNECTOR_TOKEN),
            ("TRADEGRID_API_BASE_URL", API_BASE),
        ) if not value
    ]
    if missing:
        raise SystemExit("Missing required configuration: " + ", ".join(missing))


def connect_terminal() -> None:
    initialized = mt5.initialize(path=TERMINAL_PATH) if TERMINAL_PATH else mt5.initialize()
    if not initialized:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    info = mt5.account_info()
    if info is None:
        raise RuntimeError(
            "MT5 terminal initialized but no trading account is logged in: "
            f"{mt5.last_error()}"
        )
    log.info(
        "Connected to MT5 account login=%s server=%s currency=%s (read-only bridge)",
        info.login, info.server, info.currency,
    )


def utc_from_epoch(seconds: int) -> datetime:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


def entry_name(entry: int) -> str | None:
    return {
        mt5.DEAL_ENTRY_IN: "in",
        mt5.DEAL_ENTRY_OUT: "out",
        mt5.DEAL_ENTRY_INOUT: "inout",
        mt5.DEAL_ENTRY_OUT_BY: "out_by",
    }.get(entry)


def position_direction(deal_type: int) -> str | None:
    if deal_type == mt5.DEAL_TYPE_BUY:
        return "buy"
    if deal_type == mt5.DEAL_TYPE_SELL:
        return "sell"
    return None


def deal_to_payload(deal: Any, directions: dict[str, str]) -> dict[str, Any] | None:
    entry = entry_name(int(deal.entry))
    if entry is None:
        # Ignore balance, credit, commission-only and other non-trade records.
        return None

    position_id = str(int(deal.position_id))
    direction = directions.get(position_id)
    current_type_direction = position_direction(int(deal.type))
    if entry == "in":
        direction = current_type_direction
        if direction:
            directions[position_id] = direction
    elif entry in ("out", "out_by") and direction is None:
        # On a normal close, the deal type is opposite the original position:
        # selling closes a buy; buying closes a sell. This lets incremental
        # sync include a close even when its opening deal predates the overlap.
        direction = {"buy": "sell", "sell": "buy"}.get(current_type_direction or "")
        if direction:
            directions[position_id] = direction
    elif entry == "inout" and direction is None:
        # Reversal legs are not yet represented by the trade-position schema.
        # Preserve a usable direction in the raw audit record; the aggregator
        # deliberately excludes positions containing inout until leg support.
        direction = directions.get(position_id) or current_type_direction

    if not direction:
        log.warning("Skipping deal %s: direction unavailable for position %s", deal.ticket, position_id)
        return None

    deal_time = utc_from_epoch(int(deal.time))
    return {
        "mt5_deal_id": str(int(deal.ticket)),
        "mt5_order_id": str(int(deal.order)) if getattr(deal, "order", 0) else None,
        "mt5_position_id": position_id,
        "entry_type": entry,
        "symbol": str(deal.symbol),
        "direction": direction,
        "volume": float(deal.volume),
        "price": float(deal.price),
        "profit": float(deal.profit),
        "commission": float(deal.commission),
        "swap": float(deal.swap),
        "deal_time": deal_time.isoformat(),
    }


def fetch_deals(start: datetime, end: datetime) -> list[Any]:
    # MT5 Python API expects datetime values; UTC-aware values are supported.
    deals = mt5.history_deals_get(start, end)
    if deals is None:
        raise RuntimeError(f"MT5 history_deals_get failed: {mt5.last_error()}")
    return sorted(deals, key=lambda item: (int(item.time), int(item.ticket)))


def upload_batch(client: httpx.Client, deals: list[dict[str, Any]]) -> None:
    info = mt5.account_info()
    if info is None:
        raise RuntimeError(f"MT5 account_info failed: {mt5.last_error()}")

    response = client.post(
        f"{API_BASE}/api/v1/connector/deals",
        headers={"X-Connector-Token": CONNECTOR_TOKEN},
        json={
            "deals": deals,
            "balance": float(info.balance),
            "equity": float(info.equity),
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        # Do not print the token or request headers.
        raise RuntimeError(
            f"Backend rejected deal batch ({response.status_code}): "
            f"{response.text[:500]}"
        )
    log.info("Backend accepted %s deals: %s", len(deals), response.json())


def load_sync_checkpoint(now: datetime) -> datetime:
    """Resume from the last fully uploaded history interval, with overlap."""
    try:
        data = json.loads(SYNC_STATE_PATH.read_text(encoding="utf-8"))
        if data.get("account_id") == ACCOUNT_ID and data.get("last_successful_end"):
            checkpoint = datetime.fromisoformat(str(data["last_successful_end"]).replace("Z", "+00:00"))
            if checkpoint.tzinfo is None or checkpoint.utcoffset() is None:
                checkpoint = checkpoint.replace(tzinfo=timezone.utc)
            return checkpoint.astimezone(timezone.utc) - timedelta(days=OVERLAP_DAYS)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    # Initial import starts far enough back to cover normal MT5 account history.
    # Brokers/terminals may retain less history; coverage must be reconciled.
    return now - timedelta(days=INITIAL_LOOKBACK_DAYS)


def save_sync_checkpoint(end: datetime) -> None:
    """Persist only non-secret cursor state after a complete interval upload."""
    SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = SYNC_STATE_PATH.with_suffix(SYNC_STATE_PATH.suffix + ".tmp")
    temp_path.write_text(json.dumps({
        "account_id": ACCOUNT_ID,
        "last_successful_end": end.astimezone(timezone.utc).isoformat(),
    }), encoding="utf-8")
    os.replace(temp_path, SYNC_STATE_PATH)


def sync_window(client: httpx.Client, start: datetime, end: datetime) -> int:
    """Backfill a long period in bounded windows, checkpointing each successful window."""
    cursor = start
    sent = 0
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=HISTORY_CHUNK_DAYS), end)
        terminal_deals = fetch_deals(cursor, chunk_end)
        directions: dict[str, str] = {}
        payloads: list[dict[str, Any]] = []
        for deal in terminal_deals:
            payload = deal_to_payload(deal, directions)
            if payload is not None:
                payloads.append(payload)

        for offset in range(0, len(payloads), BATCH_SIZE):
            chunk = payloads[offset:offset + BATCH_SIZE]
            upload_batch(client, chunk)
            sent += len(chunk)

        if not payloads and cursor == start:
            # Keep account balance/equity and connection health current even
            # when the requested interval contains no trades.
            upload_batch(client, [])

        # Do not advance a checkpoint until every batch in this date interval
        # has been accepted by the backend. Replayed overlap is idempotent.
        save_sync_checkpoint(chunk_end)
        cursor = chunk_end

    return sent


def main() -> None:
    require_config()
    connect_terminal()
    start = load_sync_checkpoint(datetime.now(timezone.utc))
    log.info("Starting history sync from %s; state file=%s", start.isoformat(), SYNC_STATE_PATH.name)

    try:
        with httpx.Client() as client:
            while True:
                try:
                    end = datetime.now(timezone.utc)
                    sent = sync_window(client, start, end)
                    save_sync_checkpoint(end)
                    log.info("Sync complete: %s deals submitted", sent)
                    # Re-read an overlap window so late-arriving deals are caught.
                    start = end - timedelta(days=OVERLAP_DAYS)
                except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
                    log.error("Sync failed; will resume from the last checkpoint: %s", exc)
                    start = load_sync_checkpoint(datetime.now(timezone.utc))
                time.sleep(POLL_SECONDS)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
