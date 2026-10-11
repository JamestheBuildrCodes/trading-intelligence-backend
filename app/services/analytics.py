from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Callable

from supabase import Client


PAGE_SIZE = 500


def _fetch_all_pages(query_factory: Callable[[], object], page_size: int = PAGE_SIZE) -> list[dict]:
    """Fetch every matching row with PostgREST range pagination."""
    if page_size < 1:
        raise ValueError("page_size must be positive")
    rows: list[dict] = []
    offset = 0
    while True:
        response = query_factory().range(offset, offset + page_size - 1).execute()
        page = response.data or []
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += len(page)


def _closed_trades(db: Client, account_id: str) -> list[dict]:
    return _fetch_all_pages(lambda: (
        db.table("trades")
        .select("*")
        .eq("account_id", account_id)
        .not_.is_("exit_at", "null")
        .order("exit_at")
    ))


def compute_overview(db: Client, account_id: str) -> dict:
    account_result = (
        db.table("trading_accounts")
        .select("current_balance, current_equity, currency, initial_balance, risk_mode, risk_value")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    if not account_result.data:
        raise ValueError("Account not found")
    account = account_result.data[0]

    trades = _closed_trades(db, account_id)
    trade_count = len(trades)

    if trade_count == 0:
        return {
            "balance": account["current_balance"],
            "equity": account["current_equity"],
            "today_pl": 0.0,
            "total_pl": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy_r": 0.0,
            "max_drawdown_pct": 0.0,
            "currency": account["currency"],
            "trade_count": 0,
        }

    def net(t: dict) -> float:
        return t["profit"] + t.get("commission", 0) + t.get("swap", 0)

    nets = [net(t) for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]

    total_pl = sum(nets)
    win_rate = (len(wins) / trade_count) * 100

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float(gross_profit > 0)
    )

    # Expectancy in R requires a defined risk-per-trade. Only computed for
    # fixed-$ risk accounts for now — percentage/stop-derived risk needs
    # trade-level risk capture that isn't wired up yet (see backend README).
    if account["risk_mode"] == "fixed" and account["risk_value"] > 0:
        expectancy_r = (total_pl / trade_count) / account["risk_value"]
    else:
        expectancy_r = 0.0

    today = datetime.now(timezone.utc).date()
    today_pl = sum(
        net(t) for t in trades
        if datetime.fromisoformat(t["exit_at"].replace("Z", "+00:00")).date() == today
    )

    running = account["initial_balance"]
    peak = running
    max_dd_pct = 0.0
    for t in trades:
        running += net(t)
        peak = max(peak, running)
        if peak > 0:
            dd = (peak - running) / peak * 100
            max_dd_pct = max(max_dd_pct, dd)

    return {
        "balance": account["current_balance"],
        "equity": account["current_equity"],
        "today_pl": today_pl,
        "total_pl": total_pl,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy_r": expectancy_r,
        "max_drawdown_pct": -max_dd_pct,
        "currency": account["currency"],
        "trade_count": trade_count,
    }



def summarize_trading_dna(trades: list[dict], minimum_sample_size: int = 30) -> dict:
    """Summarize closed trades by session and symbol without overstating small samples."""
    def net(trade: dict) -> float:
        return (
            float(trade.get("profit") or 0)
            + float(trade.get("commission") or 0)
            + float(trade.get("swap") or 0)
        )

    def summarize(key: str) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for trade in trades:
            raw = trade.get(key)
            label = str(raw).strip() if raw is not None and str(raw).strip() else "unknown"
            if key == "symbol" and label != "unknown":
                label = label.upper()
            groups.setdefault(label, []).append(trade)

        output = []
        for label, rows in groups.items():
            values = [net(row) for row in rows]
            count = len(values)
            output.append({
                "key": label,
                "trade_count": count,
                "win_rate": (sum(value > 0 for value in values) / count * 100) if count else 0.0,
                "net_pl": sum(values),
                "expectancy_usd": (sum(values) / count) if count else 0.0,
            })
        return sorted(output, key=lambda row: (-row["trade_count"], -row["net_pl"], row["key"]))

    sample_size = len(trades)
    ready = sample_size >= minimum_sample_size
    headline = (
        "Not enough closed trades yet. Keep journaling until the sample reaches "
        f"{minimum_sample_size} trades."
        if not ready
        else "Review the session and symbol breakdowns to identify repeatable strengths and weaknesses."
    )
    return {
        "ready": ready,
        "sample_size": sample_size,
        "minimum_sample_size": minimum_sample_size,
        "headline": headline,
        "by_session": summarize("session"),
        "by_symbol": summarize("symbol"),
    }


def compute_trading_dna(db: Client, account_id: str, minimum_sample_size: int = 30) -> dict:
    trades = _fetch_all_pages(lambda: (
        db.table("trades")
        .select("symbol,profit,commission,swap,session,exit_at")
        .eq("account_id", account_id)
        .not_.is_("exit_at", "null")
        .order("exit_at")
    ))
    return summarize_trading_dna(trades, minimum_sample_size)


WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def summarize_performance_breakdown(trades: list[dict], timezone_name: str = "UTC") -> dict:
    """Group all supplied closed trades; report imported coverage honestly."""
    try:
        account_tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        account_tz = ZoneInfo("UTC")

    groups: dict[str, dict[str, dict]] = {
        "by_symbol": {}, "by_weekday": {}, "by_hour": {}, "by_session": {}
    }

    def record(group_name: str, key: str, pnl: float) -> None:
        label = key.strip() if key and key.strip() else "Unknown"
        bucket = groups[group_name].setdefault(
            label, {"key": label, "trade_count": 0, "wins": 0, "net_pl": 0.0}
        )
        bucket["trade_count"] += 1
        bucket["wins"] += int(pnl > 0)
        bucket["net_pl"] += pnl

    for trade in trades:
        pnl = (
            float(trade.get("profit") or 0)
            + float(trade.get("commission") or 0)
            + float(trade.get("swap") or 0)
        )
        symbol = str(trade.get("symbol") or "Unknown").upper()
        session = str(trade.get("session") or "Unknown")
        record("by_symbol", symbol, pnl)
        record("by_session", session, pnl)
        exited = trade.get("exit_at")
        if exited:
            moment = exited if isinstance(exited, datetime) else datetime.fromisoformat(str(exited).replace("Z", "+00:00"))
            if moment.tzinfo is None or moment.utcoffset() is None:
                moment = moment.replace(tzinfo=timezone.utc)
            local = moment.astimezone(account_tz)
            record("by_weekday", WEEKDAY_NAMES[local.weekday()], pnl)
            record("by_hour", f"{local.hour:02d}:00–{local.hour:02d}:59", pnl)
        else:
            record("by_weekday", "Unknown", pnl)
            record("by_hour", "Unknown", pnl)

    def finalize(rows: dict[str, dict]) -> list[dict]:
        result_rows = []
        for row in rows.values():
            count = row["trade_count"]
            result_rows.append({
                **row,
                "win_rate": (row["wins"] / count * 100.0) if count else 0.0,
                "expectancy": (row["net_pl"] / count) if count else 0.0,
            })
        return sorted(result_rows, key=lambda row: (-row["net_pl"], row["key"]))

    count = len(trades)
    first = trades[0].get("exit_at") if count else None
    last = trades[-1].get("exit_at") if count else None
    return {
        "sample_size": count,
        "timezone": account_tz.key,
        "history": {
            "trade_count": count,
            "first_closed_at": first,
            "last_closed_at": last,
            "status": "no_closed_trades_imported" if not count else "imported_history_completeness_unverified",
            "note": "Coverage reflects closed trades currently imported into TradeGrid. Completeness against the broker statement must be reconciled separately.",
        },
        **{name: finalize(rows) for name, rows in groups.items()},
    }


def compute_performance_breakdown(db: Client, account_id: str) -> dict:
    account = (
        db.table("trading_accounts")
        .select("timezone")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    if not account.data:
        raise ValueError("Account not found")
    timezone_name = account.data[0].get("timezone") or "UTC"
    trades = _closed_trades(db, account_id)
    return summarize_performance_breakdown(trades, timezone_name)
