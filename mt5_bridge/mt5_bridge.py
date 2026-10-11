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


def cash_flow_to_payload(deal: Any) -> dict[str, Any] | None:
    """Map MT5 balance operations to idempotent external cash-flow records.

    Brokers may also use balance operations for adjustments, so the note
    preserves the MT5 ticket and users should verify unusual entries.
    """
    balance_type = getattr(mt5, "DEAL_TYPE_BALANCE", None)
    if balance_type is None or int(deal.type) != int(balance_type):
        return None
    amount = float(deal.profit)
    if amount == 0:
        return None
    ticket = str(int(deal.ticket))
    return {
        "external_id": ticket,
        "flow_type": "deposit" if amount > 0 else "withdrawal",
        "amount": abs(amount),
        "occurred_at": utc_from_epoch(int(deal.time)).isoformat(),
        "note": f"Imported MT5 balance operation #{ticket}; verify broker classification",
    }


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
        # On normal closes the deal side is opposite the original position.
        # This is necessary when a new sync window contains only an exit.
        direction = {"buy": "sell", "sell": "buy"}.get(current_type_direction or "")
        if direction:
            directions[position_id] = direction
    elif entry == "inout" and direction is None:
        # Reversal legs remain unsupported by the position aggregation schema;
        # retain a source-side direction for audit instead of silently guessing a completed trade.
        direction = current_type_direction

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


def upload_batch(
    client: httpx.Client,
    deals: list[dict[str, Any]],
    cash_flows: list[dict[str, Any]] | None = None,
) -> None:
    info = mt5.account_info()
    if info is None:
        raise RuntimeError(f"MT5 account_info failed: {mt5.last_error()}")

    response = client.post(
        f"{API_BASE}/api/v1/connector/deals",
        headers={"X-Connector-Token": CONNECTOR_TOKEN},
        json={
            "deals": deals,
            "cash_flows": cash_flows or [],
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


def upload_positions_snapshot(client: httpx.Client) -> int:
    """Publish a complete read-only snapshot of currently open MT5 positions."""
    info = mt5.account_info()
    if info is None:
        raise RuntimeError(f"MT5 account_info failed: {mt5.last_error()}")
    terminal_positions = mt5.positions_get()
    if terminal_positions is None:
        raise RuntimeError(f"MT5 positions_get failed: {mt5.last_error()}")

    positions: list[dict[str, Any]] = []
    for position in terminal_positions:
        direction = "buy" if int(position.type) == int(mt5.POSITION_TYPE_BUY) else "sell"
        opened_at = utc_from_epoch(int(position.time))
        stop_loss = float(position.sl) if float(getattr(position, "sl", 0) or 0) > 0 else None
        take_profit = float(position.tp) if float(getattr(position, "tp", 0) or 0) > 0 else None
        risk_to_stop = None
        if stop_loss is not None:
            order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
            calculated = mt5.order_calc_profit(
                order_type,
                str(position.symbol),
                float(position.volume),
                float(position.price_open),
                stop_loss,
            )
            if calculated is not None:
                # Risk is loss at the recorded stop, denominated by MT5 in
                # this trading account's currency. A profitable stop is zero risk.
                risk_to_stop = round(max(0.0, -float(calculated)), 4)

        positions.append({
            "mt5_position_id": str(int(position.ticket)),
            "symbol": str(position.symbol),
            "direction": direction,
            "volume": float(position.volume),
            "open_price": float(position.price_open),
            "current_price": float(position.price_current),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "unrealized_profit": float(position.profit),
            "swap": float(position.swap),
            "risk_to_stop": risk_to_stop,
            "opened_at": opened_at.isoformat(),
        })

    response = client.post(
        f"{API_BASE}/api/v1/connector/positions",
        headers={"X-Connector-Token": CONNECTOR_TOKEN},
        json={
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "currency": str(info.currency).upper(),
            "balance": float(info.balance),
            "equity": float(info.equity),
            "broker_login": str(info.login),
            "broker_server": str(info.server or ""),
            "positions": positions,
        },
        timeout=45.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Backend rejected current-position snapshot ({response.status_code}): "
            f"{response.text[:500]}"
        )
    log.info("Current-position snapshot accepted: %s", response.json())
    return len(positions)


def load_sync_checkpoint(now: datetime) -> datetime:
    """Resume from the last fully uploaded time window, with an overlap."""
    try:
        data = json.loads(SYNC_STATE_PATH.read_text(encoding="utf-8"))
        if data.get("account_id") == ACCOUNT_ID and data.get("last_successful_end"):
            checkpoint = datetime.fromisoformat(str(data["last_successful_end"]).replace("Z", "+00:00"))
            if checkpoint.tzinfo is None or checkpoint.utcoffset() is None:
                checkpoint = checkpoint.replace(tzinfo=timezone.utc)
            return checkpoint.astimezone(timezone.utc) - timedelta(days=OVERLAP_DAYS)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    # The server/terminal history filter still determines the earliest available deal.
    return now - timedelta(days=INITIAL_LOOKBACK_DAYS)


def save_sync_checkpoint(end: datetime) -> None:
    """Persist only non-secret cursor state after a successful window."""
    SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = SYNC_STATE_PATH.with_suffix(SYNC_STATE_PATH.suffix + ".tmp")
    temp_path.write_text(json.dumps({
        "account_id": ACCOUNT_ID,
        "last_successful_end": end.astimezone(timezone.utc).isoformat(),
    }), encoding="utf-8")
    os.replace(temp_path, SYNC_STATE_PATH)


def sync_window(client: httpx.Client, start: datetime, end: datetime) -> int:
    """Backfill/sync time windows without loading the entire account history in memory."""
    cursor = start
    sent = 0
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=HISTORY_CHUNK_DAYS), end)
        terminal_deals = fetch_deals(cursor, chunk_end)
        directions: dict[str, str] = {}
        payloads: list[dict[str, Any]] = []
        cash_flows: list[dict[str, Any]] = []

        for deal in terminal_deals:
            cash_flow = cash_flow_to_payload(deal)
            if cash_flow is not None:
                cash_flows.append(cash_flow)
            payload = deal_to_payload(deal, directions)
            if payload is not None:
                payloads.append(payload)

        # Keep cash-flow batches bounded by the backend schema's max length.
        for offset in range(0, len(cash_flows), 1000):
            upload_batch(client, [], cash_flows[offset:offset + 1000])

        for offset in range(0, len(payloads), BATCH_SIZE):
            chunk = payloads[offset:offset + BATCH_SIZE]
            upload_batch(client, chunk, [])
            sent += len(chunk)

        # An empty first window still refreshes balance/equity and health. Cash
        # flows were already uploaded above and are idempotent by account/ticket.
        if not payloads and not cash_flows and cursor == start:
            upload_batch(client, [], [])

        # Advance only after every request in this window has succeeded. If a
        # later batch fails, this window is replayed; source IDs make it safe.
        save_sync_checkpoint(chunk_end)
        cursor = chunk_end

    return sent


def main() -> None:
    require_config()
    connect_terminal()
    start = load_sync_checkpoint(datetime.now(timezone.utc))
    log.info("Starting history sync from %s; checkpoint=%s", start.isoformat(), SYNC_STATE_PATH.name)

    try:
        with httpx.Client() as client:
            while True:
                # Position telemetry is independent of historical imports so a
                # long initial backfill does not postpone live-risk snapshots.
                try:
                    upload_positions_snapshot(client)
                except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
                    log.error("Open-position sync failed; historical sync will still run: %s", exc)

                try:
                    end = datetime.now(timezone.utc)
                    sent = sync_window(client, start, end)
                    save_sync_checkpoint(end)
                    log.info("Historical sync complete: %s deals submitted", sent)
                    start = end - timedelta(days=OVERLAP_DAYS)
                except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
                    log.error("History sync failed; resuming from last successful checkpoint: %s", exc)
                    start = load_sync_checkpoint(datetime.now(timezone.utc))
                time.sleep(POLL_SECONDS)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
