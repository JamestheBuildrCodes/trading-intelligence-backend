from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.analytics import (
    _fetch_all_pages,
    compute_overview,
    compute_trading_dna,
    summarize_performance_breakdown,
)


class FakeQuery:
    def __init__(self, db, table):
        self.db = db
        self.table_name = table
        self.filters = {}
        self.not_null = set()
        self.order_key = None
        self.descending = False
        self.page_range = None
        self.row_limit = None

    def select(self, _columns):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    @property
    def not_(self):
        return self

    def is_(self, key, value):
        if value == "null":
            self.not_null.add(key)
        return self

    def order(self, key, desc=False):
        self.order_key = key
        self.descending = desc
        return self

    def range(self, start, end):
        self.page_range = (start, end)
        return self

    def limit(self, limit):
        self.row_limit = limit
        return self

    def execute(self):
        rows = [
            dict(row)
            for row in self.db.tables.get(self.table_name, [])
            if all(row.get(key) == value for key, value in self.filters.items())
            and all(row.get(key) is not None for key in self.not_null)
        ]
        if self.order_key:
            rows.sort(key=lambda row: row.get(self.order_key) or "", reverse=self.descending)
        if self.page_range is not None:
            start, end = self.page_range
            rows = rows[start:end + 1]
        elif self.row_limit is not None:
            rows = rows[:self.row_limit]
        return SimpleNamespace(data=rows)


class FakeDB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return FakeQuery(self, name)


def generated_trades(count):
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "id": str(index),
            "account_id": "account-1",
            "symbol": "EURUSD" if index % 2 == 0 else "XAUUSD",
            "direction": "buy",
            "session": "london",
            "profit": 1.0,
            "commission": -0.1,
            "swap": -0.05,
            "exit_at": (start + timedelta(minutes=index)).isoformat(),
        }
        for index in range(count)
    ]


def test_paginated_loader_returns_more_than_multiple_database_pages():
    rows = [{"n": index} for index in range(1205)]

    class Query:
        def __init__(self):
            self.start = 0
            self.end = 0

        def range(self, start, end):
            self.start, self.end = start, end
            return self

        def execute(self):
            return SimpleNamespace(data=rows[self.start:self.end + 1])

    loaded = _fetch_all_pages(Query, page_size=500)
    assert len(loaded) == 1205
    assert loaded[0]["n"] == 0
    assert loaded[-1]["n"] == 1204


def test_overview_and_trading_dna_analyze_all_trades_not_only_first_page():
    trades = generated_trades(1205)
    account = {
        "id": "account-1",
        "current_balance": 11000,
        "current_equity": 11000,
        "currency": "USD",
        "initial_balance": 10000,
        "risk_mode": "fixed",
        "risk_value": 100,
        "timezone": "UTC",
    }
    db = FakeDB({"trades": trades, "trading_accounts": [account]})

    overview = compute_overview(db, "account-1")
    dna = compute_trading_dna(db, "account-1")
    breakdown = summarize_performance_breakdown(trades, "UTC")

    assert overview["trade_count"] == 1205
    assert overview["total_pl"] == pytest.approx(1205 * 0.85)
    assert dna["sample_size"] == 1205
    assert breakdown["sample_size"] == 1205
    assert breakdown["history"]["trade_count"] == 1205
    assert sum(group["trade_count"] for group in breakdown["by_symbol"]) == 1205
    assert sum(group["trade_count"] for group in breakdown["by_weekday"]) == 1205


def test_performance_breakdown_marks_imported_history_as_unverified():
    breakdown = summarize_performance_breakdown(generated_trades(2), "Africa/Lagos")
    assert breakdown["history"]["status"] == "imported_history_completeness_unverified"
    assert "reconciled separately" in breakdown["history"]["note"]


def test_empty_performance_breakdown_is_explicit():
    breakdown = summarize_performance_breakdown([], "UTC")
    assert breakdown["sample_size"] == 0
    assert breakdown["history"]["status"] == "no_closed_trades_imported"
    assert breakdown["by_symbol"] == []
