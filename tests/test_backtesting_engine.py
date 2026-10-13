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


def test_entry_is_next_candle_open_and_target_hit():
    bars = candles([(100, 101, 99, 100), (100, 102, 99, 101), (101, 106, 100, 104)])
    trade = run_single_trade(bars, BacktestConfig(entry_index=0, stop_loss=98, take_profit=105))
    assert trade["entry_price"] == 100
    assert trade["entry_index"] == 0
    assert trade["exit_reason"] == "take_profit"
    assert trade["exit_price"] == 105


def test_stop_wins_if_stop_and_target_touched_same_candle():
    bars = candles([(100, 101, 99, 100), (100, 101, 99, 100), (100, 106, 97, 102)])
    trade = run_single_trade(bars, BacktestConfig(entry_index=0, stop_loss=98, take_profit=105))
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_price"] == 98


def test_commission_and_slippage_are_charged():
    bars = candles([(100, 101, 99, 100), (100, 103, 99, 102), (102, 103, 101, 102)])
    trade = run_single_trade(bars, BacktestConfig(entry_index=0, stop_loss=98, take_profit=105,
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
