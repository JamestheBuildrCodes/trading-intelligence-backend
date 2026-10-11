from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from supabase import Client

from app.db import get_supabase
from app.deps import CurrentUser, get_current_user
from app.services.entitlements import get_user_entitlements

router = APIRouter(prefix="/api/v1", tags=["portfolio risk"])

_MAJOR_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "SEK", "NOK", "DKK", "SGD", "HKD", "CNH", "MXN", "ZAR",
}


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def _latest_complete_snapshot(db: Client, account_id: str) -> dict | None:
    result = (
        db.table("mt5_position_snapshots")
        .select("id,account_id,snapshot_at,currency,balance,equity,is_complete")
        .eq("account_id", account_id)
        .eq("is_complete", True)
        .order("snapshot_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _fx_pair(symbol: str) -> tuple[str, str] | None:
    """Recognize major FX symbols including common broker suffixes."""
    normalized = "".join(character for character in symbol.upper() if character.isalpha())
    if len(normalized) < 6:
        return None
    base, quote = normalized[:3], normalized[3:6]
    if base in _MAJOR_CURRENCIES and quote in _MAJOR_CURRENCIES and base != quote:
        return base, quote
    return None


@router.get("/portfolio/risk")
def get_portfolio_risk(
    user: CurrentUser = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Qualitative cross-account risk view; does not invent price-based correlation."""
    entitlement = get_user_entitlements(db, user.id)
    if entitlement["entitlements"].get("portfolio_risk") is not True:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail={
                "code": "plan_upgrade_required",
                "feature": "portfolio_risk",
                "current_plan": entitlement["plan"],
                "required_plan": "Pro",
                "message": "Cross-account live risk requires the Pro plan.",
            },
        )
    result = (
        db.table("trading_accounts")
        .select("id,label,account_type,broker,server,currency,current_balance,current_equity,connection_status,last_sync_at")
        .eq("user_id", user.id)
        .execute()
    )
    accounts = result.data or []
    now = datetime.now(timezone.utc)
    account_summaries: list[dict] = []
    grouped_symbols: dict[str, dict] = {}
    fx_currency_net_lots: dict[str, float] = {}
    fx_currency_pairs: dict[str, set[str]] = {}
    open_risk_by_currency: dict[str, float] = {}
    risk_known_count_by_currency: dict[str, int] = {}
    risk_total_count_by_currency: dict[str, int] = {}
    alerts: list[dict] = []
    available_snapshot_count = 0
    stale_snapshot_count = 0
    no_snapshot_count = 0

    for account in accounts:
        account_id = str(account["id"])
        snapshot = _latest_complete_snapshot(db, account_id)
        positions: list[dict] = []
        snapshot_at = None
        age_seconds = None
        is_stale = False

        if snapshot:
            available_snapshot_count += 1
            snapshot_at = _parse_datetime(snapshot.get("snapshot_at"))
            if snapshot_at:
                age_seconds = max(0, int((now - snapshot_at).total_seconds()))
                is_stale = age_seconds > 300
            if is_stale:
                stale_snapshot_count += 1
            pos_result = (
                db.table("mt5_open_positions")
                .select("mt5_position_id,symbol,direction,volume,open_price,current_price,stop_loss,take_profit,unrealized_profit,swap,risk_to_stop,opened_at")
                .eq("snapshot_id", snapshot["id"])
                .eq("account_id", account_id)
                .execute()
            )
            positions = pos_result.data or []
        else:
            no_snapshot_count += 1

        pnl = sum(_number(position.get("unrealized_profit")) for position in positions)
        swap = sum(_number(position.get("swap")) for position in positions)
        known_risks = [position for position in positions if position.get("risk_to_stop") is not None]
        currency = str(snapshot.get("currency") if snapshot else account.get("currency") or "UNKNOWN").upper()
        risk_total_count_by_currency[currency] = risk_total_count_by_currency.get(currency, 0) + len(positions)
        risk_known_count_by_currency[currency] = risk_known_count_by_currency.get(currency, 0) + len(known_risks)
        open_risk_by_currency[currency] = open_risk_by_currency.get(currency, 0.0) + sum(
            _number(position.get("risk_to_stop")) for position in known_risks
        )

        account_summaries.append({
            "account_id": account_id,
            "label": account.get("label") or "Trading account",
            "account_type": account.get("account_type") or "custom",
            "broker": account.get("broker"),
            "server": account.get("server"),
            "currency": currency,
            "snapshot_at": snapshot_at.isoformat() if snapshot_at else None,
            "snapshot_age_seconds": age_seconds,
            "snapshot_status": "stale" if is_stale else "current" if snapshot else "not_yet_received",
            "position_count": len(positions),
            "unrealized_profit": round(pnl, 4),
            "open_swap": round(swap, 4),
            "known_risk_to_stop": round(sum(_number(position.get("risk_to_stop")) for position in known_risks), 4),
            "risk_to_stop_coverage": {
                "positions_with_measured_risk": len(known_risks),
                "positions_total": len(positions),
            },
            "positions": [
                {
                    "mt5_position_id": position.get("mt5_position_id"),
                    "symbol": position.get("symbol"),
                    "direction": position.get("direction"),
                    "volume": _number(position.get("volume")),
                    "open_price": _number(position.get("open_price")),
                    "current_price": _number(position.get("current_price")),
                    "stop_loss": position.get("stop_loss"),
                    "take_profit": position.get("take_profit"),
                    "unrealized_profit": _number(position.get("unrealized_profit")),
                    "swap": _number(position.get("swap")),
                    "risk_to_stop": position.get("risk_to_stop"),
                    "opened_at": position.get("opened_at"),
                }
                for position in positions
            ],
        })

        if is_stale:
            alerts.append({
                "key": f"stale-snapshot:{account_id}",
                "severity": "warning",
                "kind": "stale_position_snapshot",
                "account_ids": [account_id],
                "title": f"Position snapshot is stale for {account.get('label') or account_id}",
                "evidence": f"Last complete position snapshot was {age_seconds} seconds ago; freshness threshold is 300 seconds.",
            })
        if not snapshot:
            alerts.append({
                "key": f"missing-snapshot:{account_id}",
                "severity": "info",
                "kind": "position_snapshot_missing",
                "account_ids": [account_id],
                "title": f"No live-position snapshot for {account.get('label') or account_id}",
                "evidence": "Connect a running MT5 bridge or supported hosted connector to enable live exposure monitoring.",
            })

        for position in positions:
            symbol = str(position.get("symbol") or "UNKNOWN").upper()
            group = grouped_symbols.setdefault(symbol, {
                "symbol": symbol,
                "position_count": 0,
                "account_ids": set(),
                "account_labels": set(),
                "buy_volume_lots": 0.0,
                "sell_volume_lots": 0.0,
            })
            group["position_count"] += 1
            group["account_ids"].add(account_id)
            group["account_labels"].add(account.get("label") or account_id)
            direction = position.get("direction")
            if direction == "buy":
                group["buy_volume_lots"] += _number(position.get("volume"))
            elif direction == "sell":
                group["sell_volume_lots"] += _number(position.get("volume"))

            pair = _fx_pair(symbol)
            if pair:
                base, quote = pair
                volume = _number(position.get("volume"))
                sign = 1.0 if direction == "buy" else -1.0
                fx_currency_net_lots[base] = fx_currency_net_lots.get(base, 0.0) + sign * volume
                fx_currency_net_lots[quote] = fx_currency_net_lots.get(quote, 0.0) - sign * volume
                fx_currency_pairs.setdefault(base, set()).add(symbol)
                fx_currency_pairs.setdefault(quote, set()).add(symbol)

    symbol_exposure = []
    for symbol, group in sorted(grouped_symbols.items()):
        group["account_ids"] = sorted(group["account_ids"])
        group["account_labels"] = sorted(group["account_labels"])
        group["buy_volume_lots"] = round(group["buy_volume_lots"], 4)
        group["sell_volume_lots"] = round(group["sell_volume_lots"], 4)
        group["directional_mix"] = (
            "mixed" if group["buy_volume_lots"] and group["sell_volume_lots"]
            else "buy_only" if group["buy_volume_lots"]
            else "sell_only" if group["sell_volume_lots"] else "unknown"
        )
        symbol_exposure.append(group)
        if len(group["account_ids"]) > 1:
            alerts.append({
                "key": f"duplicate-symbol:{symbol}",
                "severity": "warning",
                "kind": "same_symbol_multiple_accounts",
                "symbol": symbol,
                "account_ids": group["account_ids"],
                "title": f"{symbol} exposure appears in multiple accounts",
                "evidence": f"{group['position_count']} open position(s) across {len(group['account_ids'])} accounts. Position direction and size are shown separately; this is not a statistical correlation calculation.",
            })

    shared_currency_alerts = []
    for currency, pairs in sorted(fx_currency_pairs.items()):
        if len(pairs) > 1:
            exposure = round(fx_currency_net_lots.get(currency, 0.0), 4)
            shared_currency_alerts.append({
                "currency": currency,
                "pairs": sorted(pairs),
                "net_directional_lots_proxy": exposure,
                "note": "Qualitative lot-volume proxy only; contract specifications differ and this is not monetary exposure or measured correlation.",
            })
    for item in shared_currency_alerts:
        alerts.append({
            "key": f"shared-fx-currency:{item['currency']}",
            "severity": "info",
            "kind": "shared_fx_currency_exposure",
            "title": f"Multiple FX positions share {item['currency']} exposure",
            "evidence": f"Open positions across {', '.join(item['pairs'])}; estimated net direction is {item['net_directional_lots_proxy']} lots as a qualitative proxy, not normalized monetary risk.",
        })

    portfolio_positions = sum(item["position_count"] for item in account_summaries)
    return {
        "as_of": now.isoformat(),
        "currency_aggregation": "Open P/L and stop-loss risk are grouped by account currency. Different currencies are never added together.",
        "summary": {
            "accounts": len(accounts),
            "accounts_with_position_snapshots": available_snapshot_count,
            "accounts_without_position_snapshots": no_snapshot_count,
            "stale_snapshots": stale_snapshot_count,
            "open_positions": portfolio_positions,
        },
        "open_risk_to_stop_by_currency": [
            {
                "currency": currency,
                "risk_to_stop": round(value, 4),
                "positions_with_risk": risk_known_count_by_currency.get(currency, 0),
                "positions_total": risk_total_count_by_currency.get(currency, 0),
                "coverage_pct": round(100 * risk_known_count_by_currency.get(currency, 0) / risk_total_count_by_currency.get(currency, 1), 2),
                "note": "Only included when the MT5 connector can calculate account-currency loss at the recorded stop. Unknown stops/contract values are excluded, not treated as zero.",
            }
            for currency, value in sorted(open_risk_by_currency.items())
        ],
        "symbol_exposure": symbol_exposure,
        "shared_fx_currency_exposure": shared_currency_alerts,
        "accounts": account_summaries,
        "alerts": alerts,
        "methodology": {
            "staleness_threshold_seconds": 300,
            "statistical_correlation": "Not calculated; historical quote series/lookback are not configured in this endpoint.",
            "volume": "Broker-reported lots. Do not sum raw lot sizes across unlike symbols as if they were equal monetary exposure.",
        },
    }
