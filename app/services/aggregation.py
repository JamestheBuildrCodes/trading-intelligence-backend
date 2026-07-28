from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from supabase import Client

# UTC-hour session windows. Simple and DST-stable enough for MVP; refine
# later using trading_accounts.session_reference_tz if precision matters
# more than this (see docs/ADR.md, ADR-007).
SESSION_WINDOWS = [
    ("asia", 0, 9),
    ("london", 8, 17),
    ("new_york", 13, 22),
]


def classify_session(dt_utc: datetime) -> str:
    hour = dt_utc.astimezone(timezone.utc).hour
    active = [name for name, start, end in SESSION_WINDOWS if start <= hour < end]
    if len(active) > 1:
        return "overlap"
    if active:
        return active[0]
    return "off_session"


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def aggregate_position(db: Client, account_id: str, mt5_position_id: str) -> None:
    """
    Recomputes the `trades` row for one position from every raw_deals row
    seen for it so far. Idempotent — safe to call after every new deal.
    Only upserts into `trades` once the position looks fully closed
    (closing volume >= opening volume); until then it waits, since a
    partially-closed position has no accurate exit price yet.
    """
    deals_result = (
        db.table("raw_deals")
        .select("*")
        .eq("account_id", account_id)
        .eq("mt5_position_id", mt5_position_id)
        .order("deal_time")
        .execute()
    )
    deals = deals_result.data
    if not deals:
        return

    opening = [d for d in deals if d["entry_type"] == "in"]
    closing = [d for d in deals if d["entry_type"] in ("out", "out_by")]

    if not opening or not closing:
        return  # nothing to aggregate yet, or position still fully open

    opened_volume = sum(d["volume"] for d in opening)
    closed_volume = sum(d["volume"] for d in closing)

    # Tolerance for float rounding on lot sizes.
    if closed_volume + 1e-6 < opened_volume:
        return  # still partially open — wait for more closing deals

    entry_price = sum(d["price"] * d["volume"] for d in opening) / opened_volume
    exit_price = sum(d["price"] * d["volume"] for d in closing) / closed_volume
    entry_at = _parse_dt(min(d["deal_time"] for d in opening))
    exit_at = _parse_dt(max(d["deal_time"] for d in closing))

    profit = sum(d["profit"] for d in deals)
    commission = sum(d["commission"] for d in deals)
    swap = sum(d["swap"] for d in deals)

    row = {
        "account_id": account_id,
        "mt5_position_id": mt5_position_id,
        "symbol": opening[0]["symbol"],
        "direction": opening[0]["direction"],
        "volume": opened_volume,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "entry_at": entry_at.isoformat(),
        "exit_at": exit_at.isoformat(),
        "holding_seconds": int((exit_at - entry_at).total_seconds()),
        "is_partial_close": len(closing) > 1,
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "session": classify_session(entry_at),
        "day_of_week": entry_at.isoweekday() % 7,
    }

    db.table("trades").upsert(row, on_conflict="account_id,mt5_position_id").execute()
