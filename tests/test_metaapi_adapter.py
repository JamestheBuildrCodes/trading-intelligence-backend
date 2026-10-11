from datetime import datetime, timezone

from app.services.metaapi_adapter import map_metaapi_deal, map_metaapi_position, parse_provider_time


def test_maps_open_and_close_deals_to_original_position_direction():
    directions = {}
    opened, cash = map_metaapi_deal({
        "id": "deal-1", "orderId": "order-1", "positionId": "position-1",
        "entryType": "DEAL_ENTRY_IN", "type": "DEAL_TYPE_BUY",
        "symbol": "EURUSD", "volume": 0.2, "price": 1.1,
        "profit": 0, "commission": -0.7, "swap": 0,
        "time": "2026-10-01T10:00:00.000Z",
    }, directions)
    closed, cash2 = map_metaapi_deal({
        "id": "deal-2", "orderId": "order-2", "positionId": "position-1",
        "entryType": "DEAL_ENTRY_OUT", "type": "DEAL_TYPE_SELL",
        "symbol": "EURUSD", "volume": 0.2, "price": 1.11,
        "profit": 20, "commission": -0.7, "swap": -0.1,
        "time": "2026-10-01T10:05:00.000Z",
    }, directions)

    assert opened is not None and opened["direction"] == "buy"
    assert closed is not None and closed["direction"] == "buy"
    assert opened["entry_type"] == "in"
    assert closed["entry_type"] == "out"
    assert cash is None and cash2 is None


def test_maps_exit_only_incremental_window_without_original_entry():
    closing, cash = map_metaapi_deal({
        "id": "deal-close", "positionId": "old-position",
        "entryType": "DEAL_ENTRY_OUT", "type": "DEAL_TYPE_SELL",
        "symbol": "XAUUSD", "volume": 0.1, "price": 2700,
        "profit": 15, "commission": -0.5, "swap": 0,
        "time": "2026-10-11T09:00:00Z",
    }, {})
    assert closing is not None
    assert closing["direction"] == "buy"
    assert cash is None


def test_balance_operation_is_a_cash_flow_not_a_trading_deal():
    trade, cash = map_metaapi_deal({
        "id": "balance-1", "type": "DEAL_TYPE_BALANCE",
        "profit": 500, "time": "2026-10-01T00:00:00Z",
    }, {})
    assert trade is None
    assert cash == {
        "external_id": "balance-1",
        "flow_type": "deposit",
        "amount": 500.0,
        "occurred_at": "2026-10-01T00:00:00+00:00",
        "note": "Imported hosted MT5 balance operation #balance-1; verify broker classification",
    }


def test_open_position_mapping_leaves_risk_unknown_without_money_calculator():
    mapped = map_metaapi_position({
        "id": "position-1", "type": "POSITION_TYPE_SELL", "symbol": "EURUSD",
        "volume": 0.4, "openPrice": 1.1, "currentPrice": 1.09,
        "stopLoss": 1.12, "takeProfit": 1.06, "profit": 40,
        "swap": -1.2, "time": "2026-10-11T09:00:00Z",
    })
    assert mapped is not None
    assert mapped["direction"] == "sell"
    assert mapped["stop_loss"] == 1.12
    assert mapped["risk_to_stop"] is None


def test_provider_times_are_normalized_to_utc():
    parsed = parse_provider_time("2026-10-11T12:00:00+02:00")
    assert parsed == datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)
