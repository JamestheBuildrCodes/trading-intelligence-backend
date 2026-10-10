import pytest
from pydantic import ValidationError

from app.schemas import DealBatchIn, DealIn


def valid_payload(**overrides):
    payload = {
        "mt5_deal_id": "1",
        "mt5_position_id": "10",
        "entry_type": "in",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.1,
        "price": 1.1,
        "deal_time": "2026-01-01T10:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_deal_accepts_timezone_aware_timestamp():
    deal = DealIn(**valid_payload())
    assert deal.deal_time.utcoffset().total_seconds() == 0


def test_deal_rejects_naive_timestamp():
    with pytest.raises(ValidationError):
        DealIn(**valid_payload(deal_time="2026-01-01T10:00:00"))


@pytest.mark.parametrize("field,value", [("volume", float("nan")), ("price", float("inf")), ("profit", float("nan"))])
def test_deal_rejects_non_finite_numeric_values(field, value):
    with pytest.raises(ValidationError):
        DealIn(**valid_payload(**{field: value}))


def test_batch_rejects_negative_account_metrics():
    with pytest.raises(ValidationError):
        DealBatchIn(deals=[], balance=-1)
