from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from supabase import Client

from app.config import get_settings
from app.db import get_supabase
from app.deps import CurrentUser, get_current_user
from app.services.backtesting import validate_candles

router = APIRouter(prefix="/api/v1/backtesting", tags=["backtesting"])
_ALLOWED_TIMEFRAMES = {"1m", "2m", "3m", "4m", "5m", "6m", "10m", "12m", "15m", "20m", "30m", "1h", "2h", "3h", "4h", "6h", "8h", "12h", "1d", "1w", "1mn"}


@router.get("/accounts/{account_id}/candles")
async def historical_candles(
    account_id: str,
    symbol: str = Query(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$"),
    timeframe: str = Query(default="15m"),
    limit: int = Query(default=500, ge=2, le=1000),
    start_time: datetime | None = Query(default=None),
    user: CurrentUser = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Read historical OHLC candles from the user's own hosted MetaApi MT5 account."""
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(status_code=422, detail="Unsupported timeframe")
    row_result = (db.table("trading_accounts")
        .select("id,user_id,connection_provider,provider_account_id,hosted_connection_status,server")
        .eq("id", account_id).eq("user_id", user.id).limit(1).execute())
    if not row_result.data:
        raise HTTPException(status_code=404, detail="Trading account not found")
    row = row_result.data[0]
    if row.get("connection_provider") != "metaapi" or not row.get("provider_account_id"):
        raise HTTPException(status_code=409, detail="Connect this account through hosted MT5 before loading historical candles.")
    token = get_settings().metaapi_token
    if not token:
        raise HTTPException(status_code=503, detail="Historical data provider is not configured.")
    try:
        from metaapi_cloud_sdk import MetaApi
        api = MetaApi(token)
        remote = await api.metatrader_account_api.get_account(str(row["provider_account_id"]))
        connection = remote.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()
        candles = await connection.get_historical_candles(
            symbol=symbol.upper(),
            timeframe=timeframe,
            start_time=(start_time or datetime.now(timezone.utc)),
            limit=limit,
        )
        normalized = []
        for item in candles or []:
            normalized.append({
                "time": item.get("time") or item.get("brokerTime"),
                "open": item.get("open"), "high": item.get("high"),
                "low": item.get("low"), "close": item.get("close"),
                "volume": item.get("tickVolume", item.get("volume", 0)),
                "spread": item.get("spread"),
            })
        # Provider returns latest-first for historical requests. Normalize timestamps to epoch seconds.
        from app.services.metaapi_adapter import parse_provider_time
        normalized.sort(key=lambda item: parse_provider_time(item["time"]))
        payload = [{"time": int(parse_provider_time(c["time"]).timestamp()), "open": float(c["open"]),
                    "high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"]),
                    "volume": float(c.get("volume") or 0), "spread": c.get("spread")} for c in normalized]
        validate_candles(payload)
        return {"account_id": account_id, "symbol": symbol.upper(), "timeframe": timeframe,
                "source": "MetaApi hosted MT5", "count": len(payload), "candles": payload,
                "oldest_time": payload[0]["time"], "newest_time": payload[-1]["time"]}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="The provider returned invalid or incomplete candle data.") from exc
    except Exception:
        raise HTTPException(status_code=502, detail="Historical candles could not be loaded. Confirm the hosted account is deployed, synchronized, and the broker symbol is valid.") from None


from pydantic import BaseModel, Field
from app.services.backtesting import BacktestConfig, run_single_trade


class SingleTradeRunIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")
    timeframe: str = "15m"
    limit: int = Field(default=500, ge=3, le=1000)
    signal_index: int = Field(default=79, ge=0)
    direction: str = "long"
    stop_loss: float
    take_profit: float
    initial_balance: float = Field(default=10000, gt=0)
    risk_percent: float = Field(default=1, gt=0, le=100)
    spread_price: float = Field(default=0, ge=0)
    slippage_price: float = Field(default=0, ge=0)
    commission_per_unit: float = Field(default=0, ge=0)


@router.post("/accounts/{account_id}/simulate")
async def simulate_historical_trade(
    account_id: str,
    body: SingleTradeRunIn,
    user: CurrentUser = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    """Simulate a single trade against server-fetched historical broker candles."""
    if body.timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(status_code=422, detail="Unsupported timeframe")
    owned = (db.table("trading_accounts")
        .select("id,user_id,connection_provider,provider_account_id")
        .eq("id", account_id).eq("user_id", user.id).limit(1).execute())
    if not owned.data:
        raise HTTPException(status_code=404, detail="Trading account not found")
    row = owned.data[0]
    if row.get("connection_provider") != "metaapi" or not row.get("provider_account_id"):
        raise HTTPException(status_code=409, detail="Connect this account through hosted MT5 before running a backtest.")
    token = get_settings().metaapi_token
    if not token:
        raise HTTPException(status_code=503, detail="Historical data provider is not configured.")
    try:
        from metaapi_cloud_sdk import MetaApi
        from app.services.metaapi_adapter import parse_provider_time
        api = MetaApi(token)
        remote = await api.metatrader_account_api.get_account(str(row["provider_account_id"]))
        connection = remote.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()
        raw = await connection.get_historical_candles(
            symbol=body.symbol.upper(), timeframe=body.timeframe,
            start_time=datetime.now(timezone.utc), limit=body.limit)
        payload = []
        for item in raw or []:
            payload.append({"time": int(parse_provider_time(item.get("time") or item.get("brokerTime")).timestamp()),
                            "open": float(item["open"]), "high": float(item["high"]),
                            "low": float(item["low"]), "close": float(item["close"]),
                            "volume": float(item.get("tickVolume", item.get("volume", 0)) or 0)})
        payload.sort(key=lambda c: c["time"])
        validate_candles(payload)
        config = BacktestConfig(direction=body.direction, signal_index=body.signal_index,
            stop_loss=body.stop_loss, take_profit=body.take_profit,
            initial_balance=body.initial_balance, risk_percent=body.risk_percent,
            spread_price=body.spread_price, slippage_price=body.slippage_price,
            commission_per_unit=body.commission_per_unit)
        trade = run_single_trade(payload, config)
        return {"account_id": account_id, "symbol": body.symbol.upper(),
            "timeframe": body.timeframe, "source": "MetaApi hosted MT5",
            "candles_used": len(payload), "trade": trade,
            "assumptions": {"entry": "next candle open after completed signal candle",
                "same_candle_collision": "stop loss first", "starting_balance": body.initial_balance,
                "risk_percent": body.risk_percent, "spread_price": body.spread_price,
                "slippage_price": body.slippage_price, "commission_per_unit_per_side": body.commission_per_unit}}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        raise HTTPException(status_code=502, detail="Backtest failed to load or validate broker candles. Check account synchronization and symbol availability.") from None
