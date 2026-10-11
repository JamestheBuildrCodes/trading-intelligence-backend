from app.services.analytics import summarize_trading_dna


def test_trading_dna_groups_closed_trades_by_session_and_symbol():
    trades = [
        {"symbol": "EURUSD", "session": "london", "profit": 20, "commission": -1, "swap": 0},
        {"symbol": "EURUSD", "session": "london", "profit": -10, "commission": -1, "swap": 0},
        {"symbol": "XAUUSD", "session": None, "profit": 5, "commission": 0, "swap": 0},
    ]
    result = summarize_trading_dna(trades, minimum_sample_size=3)

    assert result["ready"] is True
    assert result["sample_size"] == 3
    assert result["by_session"][0]["key"] == "london"
    assert result["by_session"][0]["trade_count"] == 2
    assert result["by_session"][0]["net_pl"] == 8
    assert result["by_symbol"][0]["key"] == "EURUSD"
    assert result["by_symbol"][0]["expectancy_usd"] == 4


def test_trading_dna_marks_small_samples_not_ready():
    result = summarize_trading_dna(
        [{"symbol": "EURUSD", "session": "london", "profit": 10, "commission": 0, "swap": 0}],
        minimum_sample_size=30,
    )

    assert result["ready"] is False
    assert result["sample_size"] == 1
    assert result["minimum_sample_size"] == 30
    assert "Not enough closed trades" in result["headline"]


def test_trading_dna_includes_costs_and_handles_missing_groups():
    result = summarize_trading_dna(
        [{"symbol": None, "session": None, "profit": 10, "commission": -2, "swap": -1}],
        minimum_sample_size=1,
    )

    assert result["by_symbol"][0]["key"] == "unknown"
    assert result["by_session"][0]["key"] == "unknown"
    assert result["by_symbol"][0]["net_pl"] == 7
