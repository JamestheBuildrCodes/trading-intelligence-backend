import pytest
from app.services.backtesting import BacktestConfig, run_single_trade, summarize_trades, validate_candles


def candles(rows):
    return [{"time": i * 60, "open": o, "high": h, "low": l, "close": c}
            for i, (o, h, l, c) in enumerate(rows)]


def test_rejects_bad_ohlc_and_non_monotonic_time():
    with pytest.raises(ValueError):
        validate_candles(candles([(10, 9, 8, 9)]))
    with pytest.raises(ValueError):
        validate_candles([{"time": 60, "open": 1, "high": 2, "low": 0, "close": 1},
                          {"time": 60, "open": 1, "high": 2, "low": 0, "close": 1}])


def test_entry_occurs_at_open_after_signal_candle_and_target_hit():
    bars = candles([(100, 101, 99, 100), (100, 102, 99, 101), (101, 106, 100, 104)])
    trade = run_single_trade(bars, BacktestConfig(signal_index=0, stop_loss=98, take_profit=105))
    assert trade["entry_index"] == 1
    assert trade["entry_price"] == 100
    assert trade["exit_reason"] == "take_profit"
    assert trade["exit_price"] == 105


def test_stop_wins_if_stop_and_target_touched_same_candle():
    bars = candles([(100, 101, 99, 100), (100, 106, 97, 102), (102, 103, 101, 102)])
    trade = run_single_trade(bars, BacktestConfig(signal_index=0, stop_loss=98, take_profit=105))
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_price"] == 98


def test_commission_and_slippage_are_charged():
    bars = candles([(100, 101, 99, 100), (100, 103, 99, 102), (102, 103, 101, 102)])
    trade = run_single_trade(bars, BacktestConfig(signal_index=0, stop_loss=98, take_profit=105,
                                                  commission_per_unit=0.1, slippage_price=0.2))
    assert trade["commission"] > 0
    assert trade["net_pnl"] < trade["gross_pnl"]


def test_drawdown_and_statistics():
    stats = summarize_trades([{"net_pnl": 100, "r_multiple": 1}, {"net_pnl": -50, "r_multiple": -0.5},
                              {"net_pnl": 25, "r_multiple": 0.25}], 1000)
    assert stats["trade_count"] == 3
    assert stats["win_rate_pct"] == pytest.approx(66.6666667)
    assert stats["net_pnl"] == 75
    assert stats["max_drawdown_pct"] == pytest.approx(50 / 1100 * 100)
    assert stats["final_balance"] == 1075


def test_strategy_neutral_runner_sizes_from_updated_equity_and_skips_overlap():
    from app.services.backtesting import run_backtest
    bars = candles([
        (100, 101, 99, 100), (100, 106, 99, 104),
        (104, 105, 103, 104), (104, 105, 101, 102),
        (102, 103, 101, 102),
    ])
    signals = [
        {"signal_index": 0, "direction": "long", "stop_loss": 98, "take_profit": 105},
        {"signal_index": 1, "direction": "long", "stop_loss": 102, "take_profit": 110},
        {"signal_index": 3, "direction": "long", "stop_loss": 100, "take_profit": 110},
    ]
    result = run_backtest(bars, signals, BacktestConfig(initial_balance=1000, risk_percent=1))
    assert result["statistics"]["trade_count"] == 2
    assert result["skipped_signals"] == 1
    assert result["trades"][0]["entry_index"] == 1
    assert result["trades"][1]["entry_index"] == 4
