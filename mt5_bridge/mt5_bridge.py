"""Read-only MetaTrader 5 -> TradeGrid connector.

Runs on Windows beside a logged-in MetaTrader 5 terminal. It never places,
modifies, or closes orders. Deal uploads are idempotent on the backend.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
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
INITIAL_LOOKBACK_DAYS = max(1, int(os.getenv("MT5_INITIAL_LOOKBACK_DAYS", "30")))
POLL_SECONDS = max(15, int(os.getenv("MT5_POLL_SECONDS", "60")))
BATCH_SIZE = min(500, max(1, int(os.getenv("MT5_BATCH_SIZE", "250"))))


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
    elif direction is None:
        # History may begin after the original entry. Do not guess the position
        # direction because doing so would corrupt long/short analytics.
        log.warning("Skipping deal %s: opening direction for position %s is unknown",
                    deal.ticket, position_id)
        return None

    if not direction:
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


def sync_window(client: httpx.Client, start: datetime, end: datetime) -> int:
    terminal_deals = fetch_deals(start, end)
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

    # Empty syncs still update account balance/equity and connection status.
    if not payloads:
        upload_batch(client, [], cash_flows)
        log.info("No trade deals found; imported %s cash movements", len(cash_flows))
        return 0

    sent = 0
    for offset in range(0, len(payloads), BATCH_SIZE):
        chunk = payloads[offset:offset + BATCH_SIZE]
        # Cash-flow tickets are sent once per sync; the backend de-duplicates
        # by account and MT5 deal ticket if a later overlap includes them again.
        upload_batch(client, chunk, cash_flows if offset == 0 else [])
        sent += len(chunk)
    return sent


def main() -> None:
    require_config()
    connect_terminal()
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=INITIAL_LOOKBACK_DAYS)

    try:
        with httpx.Client() as client:
            while True:
                try:
                    end = datetime.now(timezone.utc)
                    sent = sync_window(client, start, end)
                    log.info("Sync complete: %s deals submitted", sent)
                    # Re-read an overlap window so late-arriving deal history is
                    # caught. The server's account_id + deal ID upsert is idempotent.
                    start = end - timedelta(days=2)
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    log.error("Sync failed: %s", exc)
                time.sleep(POLL_SECONDS)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
