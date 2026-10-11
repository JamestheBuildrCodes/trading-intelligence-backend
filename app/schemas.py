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
    broker: str | None = None
    server: str | None = None
    mt5_login: str | None = None
    initial_balance: float = 0
    timezone: str = "UTC"
    session_reference_tz: str = "America/New_York"
    risk_mode: str = "fixed"
    risk_value: float = 0
    risk_base: str = "initial_balance"


class AccountSettingsUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    session_reference_tz: str | None = Field(default=None, min_length=1, max_length=80)
    risk_mode: Literal["fixed", "percent"] | None = None
    risk_value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    risk_base: Literal["initial_balance", "current_balance"] | None = None


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


class TradeJournalEntryIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    setup_name: str = Field(default="", max_length=120)
    thesis: str = Field(default="", max_length=4000)
    pre_trade_emotion: str = Field(default="", max_length=80)
    post_trade_emotion: str = Field(default="", max_length=80)
    followed_plan: bool | None = None
    planned_risk_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    planned_reward_r: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    mistake_tags: list[str] = Field(default_factory=list, max_length=20)
    review_notes: str = Field(default="", max_length=4000)


class TradeJournalEntryOut(TradeJournalEntryIn):
    id: str
    account_id: str
    trade_id: str
    updated_at: datetime


class PortfolioSnapshotOut(BaseModel):
    id: str
    account_id: str
    snapshot_date: str
    captured_at: datetime
    balance: float
    equity: float
    currency: str
    source: str


class CashFlowIn(BaseModel):
    flow_type: Literal["deposit", "withdrawal"]
    amount: float = Field(gt=0, allow_inf_nan=False)
    occurred_at: datetime
    note: str = Field(default="", max_length=500)

    @field_validator("occurred_at")
    @classmethod
    def require_cash_flow_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone offset")
        return value


class CashFlowOut(BaseModel):
    id: str
    account_id: str
    flow_type: Literal["deposit", "withdrawal"]
    amount: float
    occurred_at: datetime
    note: str = ""
    created_at: datetime


class PortfolioGrowthOut(BaseModel):
    currency: str
    tracking_started_at: datetime | None
    latest_snapshot_at: datetime | None
    starting_equity: float | None
    latest_equity: float | None
    net_external_cash_flow: float
    return_pct: float | None
    snapshots: list[PortfolioSnapshotOut]
    cash_flows: list[CashFlowOut]
