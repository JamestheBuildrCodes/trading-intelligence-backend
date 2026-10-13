from app.workers.tickerall_sync import normalize_trade


def _row(**updates):
    row = {
        "ticket": "10001",
        "closeTicket": "10002",
        "symbol": "EURUSD",
        "side": "buy",
        "volume": 0.1,
        "openPrice": 1.1,
        "closePrice": 1.101,
        "profit": 10.0,
        "swap": -0.2,
        "commission": -0.5,
        "openTime": "2026-10-01T10:00:00Z",
        "closeTime": "2026-10-01T11:00:00Z",
        "complete": True,
    }
    row.update(updates)
    return row


def test_incomplete_trade_with_null_profit_is_not_misreported_as_zero():
    assert normalize_trade(_row(complete=False, profit=None), "account-1") is None


def test_null_profit_is_not_coerced_to_zero_even_if_complete_flag_is_missing():
    assert normalize_trade(_row(complete=None, profit=None), "account-1") is None


def test_null_close_ticket_can_use_stable_fallback_identity():
    trade = normalize_trade(_row(closeTicket=None), "account-1")
    assert trade is not None
    assert trade["profit"] == 10.0
    assert trade["mt5_position_id"].startswith("tickerall:10001:2026-10-01T11:00:00")
    assert trade["commission"] == -0.5
    assert trade["swap"] == -0.2
