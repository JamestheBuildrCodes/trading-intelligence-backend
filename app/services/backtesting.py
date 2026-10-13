"""Deterministic candle backtesting primitives. No broker orders are sent."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from math import isfinite as _isfinite
from typing import Literal


@dataclass(frozen=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def validate_candles(raw: list[dict]) -> list[Candle]:
    result: list[Candle] = []
    previous = -1
    for index, row in enumerate(raw):
        try:
            candle = Candle(int(row["time"]), float(row["open"]), float(row["high"]),
                            float(row["low"]), float(row["close"]),
                            float(row.get("volume", row.get("tickVolume", 0)) or 0))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid candle at index {index}") from exc
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if candle.time <= previous or candle.time < 0:
            raise ValueError(f"Candle times must be non-negative and strictly increasing (index {index})")
        if not all(isfinite(value) for value in values) or min(values) < 0:
            raise ValueError(f"Candle values must be finite and non-negative (index {index})")
        if candle.low > min(candle.open, candle.close) or candle.high < max(candle.open, candle.close) or candle.low > candle.high:
            raise ValueError(f"OHLC relationship invalid at index {index}")
        previous = candle.time
        result.append(candle)
    if not result:
        raise ValueError("At least one candle is required")
    return result


@dataclass(frozen=True)
class BacktestConfig:
    direction: Literal["long", "short"] = "long"
    signal_index: int = 0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    initial_balance: float = 10000.0
    risk_percent: float = 1.0
    spread_price: float = 0.0
    slippage_price: float = 0.0
    commission_per_unit: float = 0.0
    max_bars: int | None = None


def run_single_trade(raw_candles: list[dict], config: BacktestConfig) -> dict:
    candles = validate_candles(raw_candles)
    entry_index = config.signal_index + 1  # Signal uses a completed bar; fill is next bar's open.
    if config.signal_index < 0 or entry_index >= len(candles):
        raise ValueError("signal_index must have a following candle for entry")
    if config.initial_balance <= 0 or not 0 < config.risk_percent <= 100:
        raise ValueError("initial_balance must be positive and risk_percent must be in (0, 100]")
    numeric_config = (config.initial_balance, config.risk_percent, config.spread_price, config.slippage_price, config.commission_per_unit)
    if not all(isfinite(float(value)) for value in numeric_config):
        raise ValueError("Configuration values must be finite")
    if min(config.spread_price, config.slippage_price, config.commission_per_unit) < 0:
        raise ValueError("Costs cannot be negative")
    if config.max_bars is not None and config.max_bars < 1:
        raise ValueError("max_bars must be positive when specified")
    if config.direction not in ("long", "short"):
        raise ValueError("direction must be long or short")
    entry_bar = candles[entry_index]
    entry = entry_bar.open
    sign = 1 if config.direction == "long" else -1
    stop, target = config.stop_loss, config.take_profit
    if (config.direction == "long" and not stop < entry < target) or (config.direction == "short" and not target < entry < stop):
        raise ValueError("Stop and target must bracket the next-candle open in the trade direction")
    risk_distance = abs(entry - stop)
    units = (config.initial_balance * config.risk_percent / 100) / risk_distance
    entry_fill = entry + sign * (config.spread_price / 2 + config.slippage_price)
    exit_price, exit_index, exit_reason = candles[-1].close, len(candles) - 1, "end_of_data"
    end = min(len(candles), entry_index + config.max_bars) if config.max_bars else len(candles)
    for index in range(entry_index, end):
        bar = candles[index]
        stop_hit = bar.low <= stop if config.direction == "long" else bar.high >= stop
        target_hit = bar.high >= target if config.direction == "long" else bar.low <= target
        # OHLC cannot resolve intrabar order: if both are hit, count the stop first.
        if stop_hit:
            exit_price, exit_index, exit_reason = stop, index, "stop_loss"
            break
        if target_hit:
            exit_price, exit_index, exit_reason = target, index, "take_profit"
            break
        exit_price, exit_index = bar.close, index
    exit_fill = exit_price - sign * (config.spread_price / 2 + config.slippage_price)
    gross = (exit_fill - entry_fill) * sign * units
    commission = config.commission_per_unit * units * 2
    net = gross - commission
    return {
        "direction": config.direction, "signal_index": config.signal_index,
        "entry_index": entry_index, "entry_time": entry_bar.time, "entry_price": entry,
        "entry_fill": entry_fill, "exit_index": exit_index, "exit_time": candles[exit_index].time,
        "exit_price": exit_price, "exit_fill": exit_fill, "exit_reason": exit_reason,
        "units": units, "gross_pnl": gross, "commission": commission,
        "net_pnl": net, "r_multiple": net / (risk_distance * units),
    }


def summarize_trades(trades: list[dict], initial_balance: float = 10000.0) -> dict:
    if initial_balance <= 0:
        raise ValueError("initial_balance must be positive")
    if not trades:
        return {"trade_count": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
                "net_pnl": 0.0, "profit_factor": 0.0, "expectancy_r": 0.0,
                "max_drawdown_pct": 0.0, "final_balance": initial_balance, "equity_curve": [initial_balance]}
    pnl = [float(t["net_pnl"]) for t in trades]
    rs = [float(t["r_multiple"]) for t in trades]
    if not all(isfinite(x) for x in pnl + rs):
        raise ValueError("Trade results must be finite")
    wins = sum(x > 0 for x in pnl)
    gross_win, gross_loss = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    equity, peak, max_dd = initial_balance, initial_balance, 0.0
    curve = [equity]
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)
        curve.append(equity)
    return {"trade_count": len(trades), "wins": wins, "losses": sum(x < 0 for x in pnl),
            "win_rate_pct": wins / len(pnl) * 100, "net_pnl": sum(pnl),
            "profit_factor": gross_win / gross_loss if gross_loss else (None if gross_win else 0.0),
            "expectancy_r": sum(rs) / len(rs), "max_drawdown_pct": max_dd,
            "final_balance": equity, "equity_curve": curve}


def run_backtest(raw_candles: list[dict], signals: list[dict], config: BacktestConfig) -> dict:
    """Run ordered strategy signals without overlapping positions or look-ahead fills.

    Each signal is a mapping with signal_index, direction, stop_loss and take_profit.
    The signal bar must be complete; execution is the following candle's open.
    """
    from dataclasses import replace

    candles = validate_candles(raw_candles)
    normalized = sorted(signals, key=lambda s: int(s["signal_index"]))
    trades: list[dict] = []
    skipped = 0
    last_exit_index = -1
    equity = config.initial_balance
    for signal in normalized:
        signal_index = int(signal["signal_index"])
        if signal_index < 0 or signal_index + 1 >= len(candles):
            skipped += 1
            continue
        if signal_index < last_exit_index:
            skipped += 1
            continue
        per_trade = replace(
            config,
            signal_index=signal_index,
            direction=signal.get("direction", config.direction),
            stop_loss=float(signal["stop_loss"]),
            take_profit=float(signal["take_profit"]),
            initial_balance=equity,
        )
        try:
            trade = run_single_trade(raw_candles, per_trade)
        except ValueError:
            skipped += 1
            continue
        trade["trade_number"] = len(trades) + 1
        trades.append(trade)
        equity += trade["net_pnl"]
        last_exit_index = int(trade["exit_index"])
    stats = summarize_trades(trades, config.initial_balance)
    return {"trades": trades, "statistics": stats, "skipped_signals": skipped,
            "execution_model": "completed-bar signal, next-bar-open entry; stop-first OHLC collision"}
