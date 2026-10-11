from datetime import datetime, timezone

from app.services.analytics import summarize_cost_analysis
from app.services.prop_rules import evaluate_prop_status


def test_cost_analysis_uses_full_history_and_marks_slippage_unmeasured():
    trades = [
        {
            "id": str(index),
            "symbol": "EURUSD" if index % 2 == 0 else "XAUUSD",
            "session": "london",
            "profit": 10.0,
            "commission": -0.5,
            "swap": -0.25,
            "spread": 2,
        }
        for index in range(1205)
    ]
    journal = [
        {"trade_id": "0", "mistake_tags": ["late-entry"], "followed_plan": False},
        {"trade_id": "1", "mistake_tags": ["late-entry", "overtrading"], "followed_plan": True},
        {"trade_id": "missing-trade", "mistake_tags": ["late-entry"], "followed_plan": False},
    ]

    result = summarize_cost_analysis(trades, journal, "USD")

    assert result["sample_size"] == 1205
    assert result["gross_pl"] == 12050
    assert result["commission_net"] == -602.5
    assert result["swap_net"] == -301.25
    assert result["net_pl_after_recorded_commission_and_swap"] == 11146.25
    assert result["data_coverage"]["slippage_status"] == "unmeasured"
    assert result["data_coverage"]["slippage_sample_size"] == 0
    assert sum(item["trade_count"] for item in result["by_symbol"]) == 1205
    late = next(item for item in result["execution_mistakes"] if item["tag"] == "late-entry")
    assert late["tagged_trade_count"] == 3
    assert late["outcome_linked_count"] == 2
    assert result["journal_coverage"]["follow_plan_reviewed"] == 2
    assert result["journal_coverage"]["follow_plan_adherence_pct"] == 50


def test_prop_status_uses_explicit_modes_and_warns_near_daily_limit():
    now = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)
    account = {
        "id": "account-1",
        "label": "Prop Challenge",
        "prop_firm": "Example Firm",
        "challenge_phase": "Evaluation",
        "currency": "USD",
        "current_equity": 9800,
        "current_balance": 9800,
        "last_sync_at": now.isoformat(),
    }
    rules = {
        "account_size": 10000,
        "daily_drawdown_limit": 5,
        "daily_drawdown_mode": "percentage",
        "max_drawdown_limit": 10,
        "max_drawdown_mode": "percentage",
        "max_drawdown_type": "static",
        "profit_target": 10,
        "profit_target_mode": "percentage",
        "minimum_trading_days": 5,
        "consistency_rule_pct": 50,
        "max_lot_size": 1.0,
        "rules_timezone": "UTC",
    }
    trades = [
        {
            "id": "1", "profit": -180, "commission": -10, "swap": -10,
            "exit_at": "2026-10-11T10:00:00+00:00",
        },
        {
            "id": "2", "profit": 50, "commission": 0, "swap": 0,
            "exit_at": "2026-10-10T10:00:00+00:00",
        },
    ]
    positions = [
        {"volume": 1.2, "unrealized_profit": -200, "swap": -50},
    ]

    result = evaluate_prop_status(account, rules, trades, positions, now.isoformat(), now)

    assert result["status"] == "advisory"
    assert result["daily_drawdown"]["limit_amount_in_account_currency"] == 500
    assert result["daily_drawdown"]["loss_used"] == 450
    assert result["daily_drawdown"]["remaining_buffer"] == 50
    assert result["daily_drawdown"]["state"] == "near_limit"
    assert result["overall_drawdown"]["static_floor"] == 9000
    assert result["overall_drawdown"]["remaining_buffer"] == 800
    assert result["max_position_size"]["state"] == "potential_breach"
    assert any("verify" in warning.lower() for warning in result["data_warnings"])


def test_prop_trailing_drawdown_is_unknown_without_high_watermark_series():
    now = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)
    account = {
        "id": "a", "label": "A", "currency": "USD",
        "current_equity": 10000, "current_balance": 10000,
        "last_sync_at": now.isoformat(),
    }
    rules = {
        "account_size": 10000, "max_drawdown_limit": 10,
        "max_drawdown_mode": "percentage", "max_drawdown_type": "trailing",
        "rules_timezone": "UTC",
    }
    result = evaluate_prop_status(account, rules, [], [], now.isoformat(), now)
    assert result["overall_drawdown"]["remaining_buffer"] is None
    assert result["overall_drawdown"]["state"] == "unknown_trailing_high_watermark_not_available"


def test_prop_status_without_rules_is_explicit():
    result = evaluate_prop_status({"id": "a"}, None, [], None, None)
    assert result["status"] == "not_configured"
    assert result["rules"] is None
