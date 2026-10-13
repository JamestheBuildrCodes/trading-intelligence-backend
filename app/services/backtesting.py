"""Deterministic, candle-based backtesting primitives. No broker orders are sent."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from math import isfinite
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
            candle = Candle(
                time=int(row["time"]),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume", row.get("tickVolume", 0)) or 0),
            )
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
    entry_index: int = 1
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
    if config.entry_index < 0 or config.entry_index + 1 >= len(candles):
        raise ValueError("entry_index must leave at least one later candle for execution")
    if config.initial_balance <= 0 or not 0 < config.risk_percent <= 100:
        raise ValueError("initial_balance must be positive and risk_percent must be in (0, 100]")
    if min(config.spread_price, config.slippage_price, config.commission_per_unit) < 0:
        raise ValueError("Costs cannot be negative")
    entry_bar = candles[config.entry_index]
    entry = entry_bar.open
    sign = 1 if config.direction == "long" else -1
    stop, target = config.stop_loss, config.take_profit
    if (config.direction == "long" and not stop < entry < target) or (config.direction == "short" and not target < entry < stop):
        raise ValueError("Stop and target must bracket the next-candle open in the trade direction")
    risk_distance = abs(entry - stop)
    risk_cash = config.initial_balance * config.risk_percent / 100
    units = risk_cash / risk_distance
    # Costs are explicit price units; spread is charged as a full round-trip price cost.
    entry_fill = entry + sign * (config.spread_price / 2 + config.slippage_price)
    exit_price = candles[-1].close
    exit_index = len(candles) - 1
    exit_reason = "end_of_data"
    end = min(len(candles), config.entry_index + 1 + config.max_bars) if config.max_bars else len(candles)
    for index in range(config.entry_index + 1, end):
        bar = candles[index]
        stop_hit = bar.low <= stop if config.direction == "long" else bar.high >= stop
        target_hit = bar.high >= target if config.direction == "long" else bar.low <= target
        # OHLC cannot establish intrabar order; conservative deterministic rule is stop first.
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
    risk_basis = risk_distance * units
    r_multiple = net / risk_basis if risk_basis else 0.0
    return {
        "direction": config.direction, "entry_index": config.entry_index,
        "entry_time": entry_bar.time, "entry_price": entry, "entry_fill": entry_fill,
        "exit_index": exit_index, "exit_time": candles[exit_index].time,
        "exit_price": exit_price, "exit_fill": exit_fill, "exit_reason": exit_reason,
        "units": units, "gross_pnl": gross, "commission": commission,
        "net_pnl": net, "r_multiple": r_multiple,
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
    gross_win = sum(x for x in pnl if x > 0)
    gross_loss = -sum(x for x in pnl if x < 0)
    equity = initial_balance
    peak = equity
    max_dd = 0.0
    curve = [equity]
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)
        curve.append(equity)
    return {"trade_count": len(trades), "wins": wins, "losses": sum(x < 0 for x in pnl),
            "win_rate_pct": wins / len(pnl) * 100, "net_pnl": sum(pnl),
            "profit_factor": gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0),
            "expectancy_r": sum(rs) / len(rs), "max_drawdown_pct": max_dd,
            "final_balance": equity, "equity_curve": curve}
