from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _rule_amount(value: Any, mode: str | None, account_size: float) -> float | None:
    if value is None:
        return None
    amount = _num(value)
    return account_size * amount / 100.0 if mode == "percentage" else amount


def _status_from_buffer(buffer: float | None, limit: float | None) -> str:
    if buffer is None or limit is None:
        return "unknown"
    if buffer < 0:
        return "potential_breach"
    if limit > 0 and buffer <= limit * 0.2:
        return "near_limit"
    return "within_configured_limit"


def evaluate_prop_status(
    account: dict,
    rules: dict | None,
    trades: list[dict],
    positions: list[dict] | None,
    snapshot_at: Any,
    now: datetime | None = None,
) -> dict:
    """Evaluate configurable advisory rule buffers; does not claim a firm pass."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not rules:
        return {
            "status": "not_configured",
            "account_id": account.get("id"),
            "prop_firm": account.get("prop_firm"),
            "challenge_phase": account.get("challenge_phase"),
            "note": "Configure and independently verify the rules for this exact account phase.",
            "rules": None,
        }

    timezone_name = str(rules.get("rules_timezone") or "UTC")
    try:
        zone = ZoneInfo(timezone_name)
        timezone_valid = True
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
        timezone_valid = False
        timezone_name = "UTC"

    size = _num(rules.get("account_size"))
    currency = str(account.get("currency") or "UNKNOWN").upper()
    latest_sync = _moment(account.get("last_sync_at"))
    sync_age = max(0, int((now - latest_sync).total_seconds())) if latest_sync else None
    latest_position_sync = _moment(snapshot_at)
    snapshot_age = max(0, int((now - latest_position_sync).total_seconds())) if latest_position_sync else None
    positions_available = positions is not None and snapshot_age is not None and snapshot_age <= 300
    equity_available = latest_sync is not None and sync_age is not None and sync_age <= 300

    today_local = now.astimezone(zone).date()
    daily_realized = 0.0
    realized_total = 0.0
    daily_by_date: dict[date, float] = {}
    trading_days: set[date] = set()
    positive_daily_pl: list[float] = []

    for trade in trades:
        pnl = _num(trade.get("profit")) + _num(trade.get("commission")) + _num(trade.get("swap"))
        realized_total += pnl
        exit_moment = _moment(trade.get("exit_at"))
        if not exit_moment:
            continue
        local_day = exit_moment.astimezone(zone).date()
        trading_days.add(local_day)
        daily_by_date[local_day] = daily_by_date.get(local_day, 0.0) + pnl
        if local_day == today_local:
            daily_realized += pnl
    positive_daily_pl = [value for value in daily_by_date.values() if value > 0]
    open_pnl = sum(_num(position.get("unrealized_profit")) + _num(position.get("swap")) for position in positions or [])
    largest_lot = max((_num(position.get("volume")) for position in positions or []), default=0.0)

    total_net_pnl = realized_total + (open_pnl if positions_available else 0.0)
    account_equity = _num(account.get("current_equity")) if equity_available else None
    account_balance = _num(account.get("current_balance")) if equity_available else None
    current_day_pnl = daily_realized + open_pnl if positions_available else None

    daily_limit = _rule_amount(rules.get("daily_drawdown_limit"), rules.get("daily_drawdown_mode"), size)
    daily_loss_used = max(0.0, -(current_day_pnl or 0.0)) if current_day_pnl is not None else None
    daily_buffer = daily_limit - daily_loss_used if daily_limit is not None and daily_loss_used is not None else None

    max_limit = _rule_amount(rules.get("max_drawdown_limit"), rules.get("max_drawdown_mode"), size)
    if rules.get("max_drawdown_type", "static") == "trailing":
        overall_floor = None
        overall_buffer = None
        overall_status = "unknown_trailing_high_watermark_not_available"
    elif max_limit is not None and account_equity is not None:
        overall_floor = size - max_limit
        overall_buffer = account_equity - overall_floor
        overall_status = _status_from_buffer(overall_buffer, max_limit)
    else:
        overall_floor = None
        overall_buffer = None
        overall_status = "unknown"

    target_amount = _rule_amount(rules.get("profit_target"), rules.get("profit_target_mode"), size)
    target_progress = (total_net_pnl / target_amount * 100.0) if target_amount and target_amount > 0 else None
    consistency_limit = _num(rules.get("consistency_rule_pct")) if rules.get("consistency_rule_pct") is not None else None
    positive_total = sum(positive_daily_pl)
    consistency_pct = (max(positive_daily_pl) / positive_total * 100.0) if positive_daily_pl and positive_total > 0 else None
    consistency_state = (
        "within_configured_limit" if consistency_pct is not None and consistency_limit is not None and consistency_pct <= consistency_limit
        else "potential_breach" if consistency_pct is not None and consistency_limit is not None
        else "unknown"
    )

    max_lots = _num(rules.get("max_lot_size")) if rules.get("max_lot_size") is not None else None
    max_lot_state = (
        "unknown" if max_lots is not None and not positions_available
        else "not_configured" if max_lots is None
        else "potential_breach" if largest_lot > max_lots
        else "within_configured_limit"
    )
    min_days = int(rules["minimum_trading_days"]) if rules.get("minimum_trading_days") is not None else None

    data_warnings = []
    if not positions_available:
        data_warnings.append("Live open-position snapshot is missing or older than five minutes. Daily P/L and open risk cannot be fully evaluated.")
    if not equity_available:
        data_warnings.append("Account equity/balance sync is missing or older than five minutes.")
    if not timezone_valid:
        data_warnings.append("The configured rule timezone was invalid; UTC was used for this calculation.")
    if rules.get("max_drawdown_type") == "trailing":
        data_warnings.append("Trailing drawdown requires a high-watermark rule/equity series that is not yet stored; no trailing buffer was invented.")
    data_warnings.append("Verify these advisory calculations against the firm's current agreement, including start-of-day baselines, cash-flow treatment, trailing rules, and consistency definitions.")

    return {
        "status": "advisory",
        "account_id": account.get("id"),
        "account_label": account.get("label"),
        "prop_firm": account.get("prop_firm"),
        "challenge_phase": account.get("challenge_phase"),
        "currency": currency,
        "as_of": now.isoformat(),
        "rules_timezone": timezone_name,
        "data_freshness": {
            "last_account_sync_at": latest_sync.isoformat() if latest_sync else None,
            "account_sync_age_seconds": sync_age,
            "position_snapshot_at": latest_position_sync.isoformat() if latest_position_sync else None,
            "position_snapshot_age_seconds": snapshot_age,
            "positions_available_and_fresh": positions_available,
        },
        "performance": {
            "closed_trade_count": len(trades),
            "trading_days_logged": len(trading_days),
            "today_realized_net_pl": round(daily_realized, 4),
            "today_open_pl_plus_swap": round(open_pnl, 4) if positions_available else None,
            "today_combined_pl": round(current_day_pnl, 4) if current_day_pnl is not None else None,
            "all_history_realized_net_pl": round(realized_total, 4),
            "open_pl_plus_swap": round(open_pnl, 4) if positions_available else None,
            "net_pl_including_open_positions": round(total_net_pnl, 4) if positions_available else None,
            "profit_target_amount": round(target_amount, 4) if target_amount is not None else None,
            "profit_target_progress_pct": round(target_progress, 2) if target_progress is not None else None,
            "largest_open_position_lots": round(largest_lot, 4) if positions_available else None,
        },
        "daily_drawdown": {
            "configured_limit": rules.get("daily_drawdown_limit"),
            "limit_mode": rules.get("daily_drawdown_mode"),
            "limit_amount_in_account_currency": round(daily_limit, 4) if daily_limit is not None else None,
            "loss_used": round(daily_loss_used, 4) if daily_loss_used is not None else None,
            "remaining_buffer": round(daily_buffer, 4) if daily_buffer is not None else None,
            "state": _status_from_buffer(daily_buffer, daily_limit) if positions_available else "unknown",
        },
        "overall_drawdown": {
            "configured_limit": rules.get("max_drawdown_limit"),
            "limit_mode": rules.get("max_drawdown_mode"),
            "drawdown_type": rules.get("max_drawdown_type"),
            "static_floor": round(overall_floor, 4) if overall_floor is not None else None,
            "current_equity": round(account_equity, 4) if account_equity is not None else None,
            "remaining_buffer": round(overall_buffer, 4) if overall_buffer is not None else None,
            "state": overall_status,
        },
        "consistency": {
            "configured_limit_pct": consistency_limit,
            "largest_positive_day_share_pct": round(consistency_pct, 2) if consistency_pct is not None else None,
            "state": consistency_state,
            "definition": "Largest positive closed-trade day divided by the sum of positive closed-trade days, in the configured timezone. Verify this definition matches the target firm.",
        },
        "minimum_trading_days": {
            "configured_minimum": min_days,
            "days_logged": len(trading_days),
            "days_remaining": max(0, min_days - len(trading_days)) if min_days is not None else None,
            "state": "not_configured" if min_days is None else "target_reached" if len(trading_days) >= min_days else "in_progress",
        },
        "max_position_size": {
            "configured_max_lots": max_lots,
            "largest_open_position_lots": round(largest_lot, 4) if positions_available else None,
            "state": max_lot_state,
            "note": "Checks each currently open position individually; it does not mean the sum of all positions is within the rule.",
        },
        "data_warnings": data_warnings,
        "rules": rules,
    }
