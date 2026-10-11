import pytest
from pydantic import ValidationError

from app.routers.risk import _fx_pair
from app.schemas import OpenPositionIn, PositionSnapshotIn, PropFirmRulesIn


def test_snapshot_accepts_complete_empty_open_position_set():
    snapshot = PositionSnapshotIn(
        snapshot_at="2026-10-11T12:00:00+00:00",
        currency="usd",
        balance=10000,
        equity=10000,
        broker_login="123456",
        broker_server="Broker-Demo",
        positions=[],
    )
    assert snapshot.currency == "USD"
    assert snapshot.positions == []


def test_open_position_requires_timezone_and_positive_volume():
    with pytest.raises(ValidationError):
        OpenPositionIn(
            mt5_position_id="1", symbol="EURUSD", direction="buy",
            volume=0, open_price=1.1, current_price=1.1,
            opened_at="2026-10-11T12:00:00",
        )


def test_fx_exposure_parser_recognizes_broker_suffix_but_not_metals():
    assert _fx_pair("EURUSD.a") == ("EUR", "USD")
    assert _fx_pair("GBPJPY-pro") == ("GBP", "JPY")
    assert _fx_pair("XAUUSD") is None
    assert _fx_pair("US30.cash") is None


def test_prop_rule_schema_requires_explicit_positive_account_size():
    with pytest.raises(ValidationError):
        PropFirmRulesIn(account_size=0)
    rule = PropFirmRulesIn(account_size=10000, daily_drawdown_limit=5, max_drawdown_limit=10)
    assert rule.daily_drawdown_mode == "percentage"
    assert rule.max_drawdown_type == "static"
