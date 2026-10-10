from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AccountCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=100)
    broker: str | None = Field(default=None, max_length=100)
    server: str | None = Field(default=None, max_length=150)
    mt5_login: str | None = Field(default=None, max_length=80)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    account_type: str = Field(default="personal", min_length=1, max_length=40)
    initial_balance: float = Field(default=0, ge=0, allow_inf_nan=False)
    timezone: str = Field(default="UTC", min_length=1, max_length=80)
    session_reference_tz: str = Field(default="America/New_York", min_length=1, max_length=80)
    risk_mode: str = Field(default="fixed", min_length=1, max_length=40)
    risk_value: float = Field(default=0, ge=0, allow_inf_nan=False)
    risk_base: str = Field(default="initial_balance", min_length=1, max_length=40)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class AccountOut(BaseModel):
    id: str
    label: str
    account_type: str
    currency: str
    current_balance: float
    current_equity: float
    connection_status: str
    last_sync_at: datetime | None = None


class ConnectorTokenOut(BaseModel):
    token: str
    expires_at: datetime


class DealIn(BaseModel):
    mt5_deal_id: str = Field(min_length=1, max_length=80)
    mt5_order_id: str | None = Field(default=None, max_length=80)
    mt5_position_id: str = Field(min_length=1, max_length=80)
    entry_type: Literal["in", "out", "inout", "out_by"]
    symbol: str = Field(min_length=1, max_length=80)
    direction: Literal["buy", "sell"]
    volume: float = Field(gt=0, allow_inf_nan=False)
    price: float = Field(ge=0, allow_inf_nan=False)
    profit: float = Field(default=0, allow_inf_nan=False)
    commission: float = Field(default=0, allow_inf_nan=False)
    swap: float = Field(default=0, allow_inf_nan=False)
    deal_time: datetime

    @field_validator("deal_time")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deal_time must include a timezone offset")
        return value


class DealBatchIn(BaseModel):
    deals: list[DealIn] = Field(max_length=1000)
    balance: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    equity: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class OverviewOut(BaseModel):
    balance: float
    equity: float
    today_pl: float
    total_pl: float
    win_rate: float
    profit_factor: float
    expectancy_r: float
    max_drawdown_pct: float
    currency: str
    trade_count: int


class TradeOut(BaseModel):
    id: str
    symbol: str
    direction: str
    volume: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    entry_at: datetime | None = None
    exit_at: datetime | None = None
    profit: float
    commission: float = 0
    swap: float = 0
    session: str | None = None
    is_partial_close: bool
    rule_violations: list[str] = Field(default_factory=list)
