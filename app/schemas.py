from datetime import datetime
from pydantic import BaseModel


class AccountCreate(BaseModel):
    label: str
    broker: str | None = None
    server: str | None = None
    mt5_login: str | None = None
    currency: str = "USD"
    account_type: str = "personal"
    initial_balance: float = 0
    timezone: str = "UTC"
    session_reference_tz: str = "America/New_York"
    risk_mode: str = "fixed"
    risk_value: float = 0
    risk_base: str = "initial_balance"


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
    token: str  # raw token — shown once, never retrievable again
    expires_at: datetime


class DealIn(BaseModel):
    mt5_deal_id: str
    mt5_order_id: str | None = None
    mt5_position_id: str
    entry_type: str  # in | out | inout | out_by
    symbol: str
    direction: str  # buy | sell
    volume: float
    price: float
    profit: float = 0
    commission: float = 0
    swap: float = 0
    deal_time: datetime


class DealBatchIn(BaseModel):
    deals: list[DealIn]
    balance: float | None = None
    equity: float | None = None


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
    session: str | None = None
    is_partial_close: bool
