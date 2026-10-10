from datetime import datetime, timezone

from app.services.aggregation import aggregate_position, classify_session


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, db, table):
        self.db = db
        self.table_name = table
        self.filters = {}

    def select(self, _columns):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def order(self, *_args, **_kwargs):
        return self

    def execute(self):
        if self.table_name == "raw_deals":
            rows = [row for row in self.db.raw_deals if all(row.get(k) == v for k, v in self.filters.items())]
            return FakeResult(rows)
        return FakeResult([])

    def upsert(self, row, **_kwargs):
        self.db.upserted.append((self.table_name, row))
        return self


class FakeDB:
    def __init__(self, raw_deals):
        self.raw_deals = raw_deals
        self.upserted = []

    def table(self, name):
        return FakeQuery(self, name)


def deal(deal_id, entry, direction, volume, price, time, profit=0):
    return {
        "mt5_deal_id": deal_id,
        "mt5_position_id": "position-1",
        "entry_type": entry,
        "direction": direction,
        "symbol": "EURUSD",
        "volume": volume,
        "price": price,
        "deal_time": time,
        "profit": profit,
        "commission": -1,
        "swap": 0,
    }


def test_aggregate_closed_position_with_partial_exits():
    db = FakeDB([
        deal("1", "in", "buy", 1.0, 1.1, "2026-01-01T10:00:00+00:00"),
        deal("2", "out", "buy", 0.4, 1.2, "2026-01-01T11:00:00+00:00", 40),
        deal("3", "out", "buy", 0.6, 1.3, "2026-01-01T12:00:00+00:00", 60),
    ])
    aggregate_position(db, "account-1", "position-1")
    assert len(db.upserted) == 1
    table, row = db.upserted[0]
    assert table == "trades"
    assert row["volume"] == 1.0
    assert abs(row["exit_price"] - 1.26) < 1e-9
    assert row["profit"] == 100
    assert row["commission"] == -3
    assert row["is_partial_close"] is True


def test_aggregate_waits_until_position_is_fully_closed():
    db = FakeDB([
        deal("1", "in", "buy", 1.0, 1.1, "2026-01-01T10:00:00+00:00"),
        deal("2", "out", "buy", 0.4, 1.2, "2026-01-01T11:00:00+00:00", 40),
    ])
    aggregate_position(db, "account-1", "position-1")
    assert db.upserted == []


def test_aggregate_does_not_silently_merge_reversal_deal():
    db = FakeDB([
        deal("1", "in", "buy", 1.0, 1.1, "2026-01-01T10:00:00+00:00"),
        deal("2", "inout", "sell", 2.0, 1.0, "2026-01-01T11:00:00+00:00", 10),
    ])
    aggregate_position(db, "account-1", "position-1")
    assert db.upserted == []


def test_classify_session_requires_timezone_aware_datetime():
    assert classify_session(datetime(2026, 1, 1, 14, tzinfo=timezone.utc)) == "overlap"
