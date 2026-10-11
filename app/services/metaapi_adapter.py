"""Normalize MetaApi RPC payloads to TradeGrid's existing MT5 deal schema.

This adapter is read-only. It never calls any trade execution methods.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_provider_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    else:
        raise ValueError("Provider deal/position timestamp is missing or invalid")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _side(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw == "buy" or "type_buy" in raw or raw.endswith("_buy"):
        return "buy"
    if raw == "sell" or "type_sell" in raw or raw.endswith("_sell"):
        return "sell"
    return None


def _entry(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw.startswith("deal_entry_"):
        raw = raw[len("deal_entry_"):]
    elif raw.startswith("entry_"):
        raw = raw[len("entry_"):]
    return raw if raw in {"in", "out", "inout", "out_by"} else None


def map_metaapi_deal(
    deal: dict[str, Any],
    directions: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (normalized trading deal, cash-flow row), if applicable."""
    deal_id = str(deal.get("id") or deal.get("ticket") or "").strip()
    type_raw = deal.get("type") or deal.get("dealType") or deal.get("deal_type")
    type_name = str(type_raw or "").strip().lower()
    amount = float(deal.get("profit") or 0)

    if "deal_type_balance" in type_name or type_name == "balance":
        cash_flow = None
        if deal_id and amount:
            cash_flow = {
                "external_id": deal_id,
                "flow_type": "deposit" if amount > 0 else "withdrawal",
                "amount": abs(amount),
                "occurred_at": parse_provider_time(deal.get("time") or deal.get("dealTime")).isoformat(),
                "note": f"Imported hosted MT5 balance operation #{deal_id}; verify broker classification",
            }
        return None, cash_flow

    entry = _entry(deal.get("entryType") or deal.get("entry_type") or deal.get("entry"))
    if entry is None:
        return None, None

    position_id = str(deal.get("positionId") or deal.get("position_id") or deal.get("position") or "").strip()
    if not deal_id or not position_id or position_id == "0":
        return None, None

    current_side = _side(type_raw)
    direction = directions.get(position_id)
    if entry == "in":
        direction = current_side
        if direction:
            directions[position_id] = direction
    elif entry in ("out", "out_by") and direction is None:
        direction = {"buy": "sell", "sell": "buy"}.get(current_side or "")
        if direction:
            directions[position_id] = direction
    elif entry == "inout" and direction is None:
        direction = current_side

    if direction not in ("buy", "sell"):
        return None, None

    try:
        volume = float(deal.get("volume") or 0)
        price = float(deal.get("price") or 0)
        if volume <= 0:
            return None, None
        if price < 0:
            return None, None
        timestamp = parse_provider_time(deal.get("time") or deal.get("dealTime"))
    except (TypeError, ValueError, OverflowError):
        return None, None

    order_id = deal.get("orderId") or deal.get("order_id") or deal.get("order")
    payload = {
        "mt5_deal_id": deal_id,
        "mt5_order_id": str(order_id) if order_id else None,
        "mt5_position_id": position_id,
        "entry_type": entry,
        "symbol": str(deal.get("symbol") or "").strip(),
        "direction": direction,
        "volume": volume,
        "price": price,
        "profit": amount,
        "commission": float(deal.get("commission") or 0),
        "swap": float(deal.get("swap") or 0),
        "deal_time": timestamp.isoformat(),
    }
    if not payload["symbol"]:
        return None, None
    return payload, None


def map_metaapi_position(position: dict[str, Any]) -> dict[str, Any] | None:
    """Map open positions; risk-to-stop remains unknown without a verified account-currency calculation."""
    position_id = str(position.get("id") or position.get("positionId") or position.get("ticket") or "").strip()
    symbol = str(position.get("symbol") or "").strip()
    direction = _side(position.get("type") or position.get("direction"))
    if not position_id or not symbol or direction not in ("buy", "sell"):
        return None

    try:
        volume = float(position.get("volume") or 0)
        open_price = float(position.get("openPrice") or position.get("open_price") or 0)
        current_price = float(position.get("currentPrice") or position.get("current_price") or 0)
        opened_at = parse_provider_time(position.get("time") or position.get("timeCreated") or position.get("openedAt"))
    except (TypeError, ValueError, OverflowError):
        return None
    if volume <= 0 or open_price < 0 or current_price < 0:
        return None

    sl = position.get("stopLoss") if position.get("stopLoss") is not None else position.get("stop_loss")
    tp = position.get("takeProfit") if position.get("takeProfit") is not None else position.get("take_profit")
    return {
        "mt5_position_id": position_id,
        "symbol": symbol,
        "direction": direction,
        "volume": volume,
        "open_price": open_price,
        "current_price": current_price,
        "stop_loss": float(sl) if sl not in (None, 0, 0.0, "") else None,
        "take_profit": float(tp) if tp not in (None, 0, 0.0, "") else None,
        "unrealized_profit": float(position.get("profit") or position.get("unrealizedProfit") or 0),
        "swap": float(position.get("swap") or 0),
        "risk_to_stop": None,
        "opened_at": opened_at.isoformat(),
    }
