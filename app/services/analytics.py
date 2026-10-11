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
        .order("id")
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
        .order("id")
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
            "note": "Coverage reflects closed trades currently imported into Tradvoryx. Completeness against the broker statement must be reconciled separately.",
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


def summarize_cost_analysis(trades: list[dict], journal_entries: list[dict], currency: str) -> dict:
    """Evidence-based cost analysis over the full imported closed-trade set."""
    total_gross = 0.0
    total_commission = 0.0
    total_swap = 0.0
    net_by_symbol: dict[str, dict] = {}
    net_by_session: dict[str, dict] = {}
    per_trade: dict[str, dict] = {}
    spread_values: list[float] = []
    commission_present = 0
    swap_present = 0

    def bump(groups: dict[str, dict], key: str, row: dict, gross: float, commission: float, swap: float, net_pl: float) -> None:
        label = key.strip() if key and key.strip() else "Unknown"
        group = groups.setdefault(label, {
            "key": label, "trade_count": 0, "gross_pl": 0.0,
            "commission_net": 0.0, "swap_net": 0.0, "net_pl": 0.0,
            "recorded_cost": 0.0,
        })
        group["trade_count"] += 1
        group["gross_pl"] += gross
        group["commission_net"] += commission
        group["swap_net"] += swap
        group["net_pl"] += net_pl
        group["recorded_cost"] += -(commission + swap)

    for trade in trades:
        trade_id = str(trade.get("id") or "")
        gross = float(trade.get("profit") or 0)
        commission = float(trade.get("commission") or 0)
        swap = float(trade.get("swap") or 0)
        net_pl = gross + commission + swap
        total_gross += gross
        total_commission += commission
        total_swap += swap
        commission_present += int(trade.get("commission") is not None)
        swap_present += int(trade.get("swap") is not None)
        spread = trade.get("spread")
        if spread is not None:
            try:
                spread_values.append(float(spread))
            except (TypeError, ValueError):
                pass
        per_trade[trade_id] = {
            "gross_pl": gross, "commission_net": commission,
            "swap_net": swap, "net_pl": net_pl,
        }
        bump(net_by_symbol, str(trade.get("symbol") or "Unknown").upper(), trade, gross, commission, swap, net_pl)
        bump(net_by_session, str(trade.get("session") or "Unknown"), trade, gross, commission, swap, net_pl)

    mistake_groups: dict[str, dict] = {}
    plan_reviewed = 0
    plan_followed = 0
    joined_journal_entries = 0
    for entry in journal_entries:
        trade_id = str(entry.get("trade_id") or "")
        trade_metrics = per_trade.get(trade_id)
        tags = entry.get("mistake_tags") or []
        if not isinstance(tags, list):
            tags = []
        if trade_metrics:
            joined_journal_entries += 1
            if entry.get("followed_plan") is not None:
                plan_reviewed += 1
                plan_followed += int(bool(entry.get("followed_plan")))
        for raw_tag in tags:
            tag = str(raw_tag).strip()
            if not tag:
                continue
            group = mistake_groups.setdefault(tag, {
                "tag": tag, "tagged_trade_count": 0, "gross_pl": 0.0,
                "commission_net": 0.0, "swap_net": 0.0, "net_pl": 0.0,
                "outcome_linked_count": 0,
            })
            group["tagged_trade_count"] += 1
            if trade_metrics:
                group["outcome_linked_count"] += 1
                for key in ("gross_pl", "commission_net", "swap_net", "net_pl"):
                    group[key] += trade_metrics[key]

    def finish_group(group: dict) -> dict:
        return {
            **group,
            "gross_pl": round(group["gross_pl"], 4),
            "commission_net": round(group["commission_net"], 4),
            "swap_net": round(group["swap_net"], 4),
            "net_pl": round(group["net_pl"], 4),
            "recorded_cost": round(group["recorded_cost"], 4),
        }

    count = len(trades)
    commission_cost = -total_commission
    swap_cost = -total_swap
    recorded_cost = commission_cost + swap_cost
    measured_spread_count = len(spread_values)
    return {
        "currency": currency,
        "sample_size": count,
        "history_scope": "all imported closed trades; broker history completeness is not independently verified",
        "gross_pl": round(total_gross, 4),
        "commission_net": round(total_commission, 4),
        "swap_net": round(total_swap, 4),
        "commission_cost_signed": round(commission_cost, 4),
        "swap_cost_signed": round(swap_cost, 4),
        "recorded_commission_and_swap_cost_signed": round(recorded_cost, 4),
        "average_recorded_commission_and_swap_cost_per_trade": round(recorded_cost / count, 4) if count else None,
        "net_pl_after_recorded_commission_and_swap": round(total_gross + total_commission + total_swap, 4),
        "data_coverage": {
            "trades": count,
            "commission_field_present": commission_present,
            "swap_field_present": swap_present,
            "spread_field_present": measured_spread_count,
            "spread_mean_in_source_units": round(sum(spread_values) / measured_spread_count, 8) if measured_spread_count else None,
            "spread_note": "Spread is shown only as stored in the connector's source units; it is not converted into money because symbol contract/quote metadata is not currently retained with each trade.",
            "slippage_status": "unmeasured",
            "slippage_sample_size": 0,
            "slippage_note": "Expected/reference execution prices and a matching fill benchmark are not stored, so actual slippage cannot be measured honestly.",
        },
        "by_symbol": [
            finish_group(group)
            for group in sorted(net_by_symbol.values(), key=lambda item: (-item["net_pl"], item["key"]))
        ],
        "by_session": [
            finish_group(group)
            for group in sorted(net_by_session.values(), key=lambda item: (-item["net_pl"], item["key"]))
        ],
        "execution_mistakes": [
            {
                **group,
                "gross_pl": round(group["gross_pl"], 4),
                "commission_net": round(group["commission_net"], 4),
                "swap_net": round(group["swap_net"], 4),
                "net_pl": round(group["net_pl"], 4),
                "outcome_note": "Outcome metrics include only journal tags matched to an imported trade ID; a tag count may include orphaned annotations.",
            }
            for group in sorted(mistake_groups.values(), key=lambda item: (-item["tagged_trade_count"], item["tag"]))
        ],
        "journal_coverage": {
            "annotations_matched_to_trades": joined_journal_entries,
            "follow_plan_reviewed": plan_reviewed,
            "follow_plan_counted_as_followed": plan_followed,
            "follow_plan_adherence_pct": round(plan_followed / plan_reviewed * 100, 2) if plan_reviewed else None,
            "note": "Mistake and plan tags are user-recorded annotations, not automatically inferred psychological states.",
        },
    }


def compute_cost_analysis(db: Client, account_id: str) -> dict:
    account_result = (
        db.table("trading_accounts")
        .select("currency")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    if not account_result.data:
        raise ValueError("Account not found")
    trades = _closed_trades(db, account_id)
    journal_entries = _fetch_all_pages(lambda: (
        db.table("trade_journal_entries")
        .select("trade_id,mistake_tags,followed_plan")
        .eq("account_id", account_id)
        .order("updated_at")
        .order("id")
    ))
    return summarize_cost_analysis(trades, journal_entries, account_result.data[0].get("currency") or "UNKNOWN")
