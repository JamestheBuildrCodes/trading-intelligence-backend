from datetime import datetime, timezone

from supabase import Client

# MVP UTC-hour session windows. A later analytics iteration should use the
# account's configured reference timezone and daylight-saving-aware market hours.
SESSION_WINDOWS = [
    ("asia", 0, 9),
    ("london", 8, 17),
    ("new_york", 13, 22),
]


def classify_session(dt_utc: datetime) -> str:
    if dt_utc.tzinfo is None or dt_utc.utcoffset() is None:
        raise ValueError("Session classification requires a timezone-aware datetime")
    hour = dt_utc.astimezone(timezone.utc).hour
    active = [name for name, start, end in SESSION_WINDOWS if start <= hour < end]
    if len(active) > 1:
        return "overlap"
    return active[0] if active else "off_session"


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Stored deal timestamps must include a timezone")
    return result


def _weighted_price(deals: list[dict]) -> float:
    volume = sum(float(d["volume"]) for d in deals)
    if volume <= 0:
        raise ValueError("Deal volume must be positive")
    return sum(float(d["price"]) * float(d["volume"]) for d in deals) / volume


def aggregate_position(db: Client, account_id: str, mt5_position_id: str) -> None:
    """Aggregate one closed position using persisted MT5 deal history.

    The initial schema uses one trade row per position ID and cannot faithfully
    model netting reversals (MT5 entry_type='inout') as separate trade legs.
    Such histories are stored for audit but deliberately excluded from derived
    metrics until a position-leg schema migration exists.
    """
    query = (
        db.table("raw_deals")
        .select("*")
        .eq("account_id", account_id)
        .eq("mt5_position_id", mt5_position_id)
        .order("deal_time")
    )
    deals = query.execute().data or []
    if not deals or any(d["entry_type"] == "inout" for d in deals):
        return

    opening = [d for d in deals if d["entry_type"] == "in"]
    closing = [d for d in deals if d["entry_type"] in ("out", "out_by")]
    if not opening or not closing:
        return

    opened_volume = sum(float(d["volume"]) for d in opening)
    closed_volume = sum(float(d["volume"]) for d in closing)
    if opened_volume <= 0 or closed_volume + 1e-6 < opened_volume:
        return

    direction = opening[0]["direction"]
    symbol = opening[0]["symbol"]
    if any(d["direction"] != direction or d["symbol"] != symbol for d in opening):
        return

    entry_at = min(_parse_dt(d["deal_time"]) for d in opening)
    exit_at = max(_parse_dt(d["deal_time"]) for d in closing)
    row = {
        "account_id": account_id,
        "mt5_position_id": mt5_position_id,
        "symbol": symbol,
        "direction": direction,
        "volume": opened_volume,
        "entry_price": _weighted_price(opening),
        "exit_price": _weighted_price(closing),
        "entry_at": entry_at.astimezone(timezone.utc).isoformat(),
        "exit_at": exit_at.astimezone(timezone.utc).isoformat(),
        "holding_seconds": max(0, int((exit_at - entry_at).total_seconds())),
        "is_partial_close": len(closing) > 1,
        "profit": sum(float(d["profit"]) for d in deals),
        "commission": sum(float(d.get("commission") or 0) for d in deals),
        "swap": sum(float(d.get("swap") or 0) for d in deals),
        "session": classify_session(entry_at),
        "day_of_week": entry_at.isoweekday() % 7,
    }
    db.table("trades").upsert(row, on_conflict="account_id,mt5_position_id").execute()
