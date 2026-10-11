from datetime import datetime, timezone

from supabase import Client


def _closed_trades(db: Client, account_id: str) -> list[dict]:
    result = (
        db.table("trades")
        .select("*")
        .eq("account_id", account_id)
        .not_.is_("exit_at", "null")
        .order("exit_at")
        .execute()
    )
    return result.data


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
    result = (
        db.table("trades")
        .select("symbol,profit,commission,swap,session,exit_at")
        .eq("account_id", account_id)
        .not_.is_("exit_at", "null")
        .order("exit_at")
        .execute()
    )
    return summarize_trading_dna(result.data or [], minimum_sample_size)
