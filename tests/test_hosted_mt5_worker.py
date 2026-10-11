from types import SimpleNamespace

import pytest

from app.workers import hosted_mt5_sync


class _Query:
    def __init__(self, table, db):
        self.table_name = table
        self.db = db
        self.payload = None
        self.operation = None

    def insert(self, payload):
        self.operation = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = payload
        return self

    def select(self, *_args):
        self.operation = "select"
        return self

    def eq(self, *_args):
        return self

    def neq(self, *_args):
        return self

    def execute(self):
        if self.table_name == "hosted_mt5_worker_runs" and self.operation == "insert":
            self.db.run_record = dict(self.payload)
            self.db.run_record["id"] = "run-1"
            return SimpleNamespace(data=[{"id": "run-1"}])
        if self.table_name == "hosted_mt5_worker_runs" and self.operation == "update":
            self.db.run_record.update(self.payload)
            return SimpleNamespace(data=[self.db.run_record])
        if self.table_name == "trading_accounts" and self.operation == "select":
            return SimpleNamespace(data=[])
        raise AssertionError(f"Unexpected database operation: {self.table_name}/{self.operation}")


class _Database:
    def __init__(self):
        self.run_record = {}

    def table(self, name):
        return _Query(name, self)


def test_worker_fails_fast_when_metaapi_token_is_missing(monkeypatch):
    monkeypatch.setattr(hosted_mt5_sync, "get_settings", lambda: SimpleNamespace(metaapi_token=None))

    with pytest.raises(RuntimeError, match="METAAPI_TOKEN is missing"):
        import asyncio
        asyncio.run(hosted_mt5_sync.run_once())


def test_worker_records_failure_when_no_hosted_accounts_are_eligible(monkeypatch):
    import asyncio

    db = _Database()
    monkeypatch.setattr(hosted_mt5_sync, "get_settings", lambda: SimpleNamespace(metaapi_token="configured-token"))
    monkeypatch.setattr(hosted_mt5_sync, "get_supabase", lambda: db)

    with pytest.raises(RuntimeError, match="no_eligible_accounts"):
        asyncio.run(hosted_mt5_sync.run_once())

    assert db.run_record["status"] == "failed"
    assert db.run_record["safe_error_code"] == "no_eligible_accounts"
    assert db.run_record["completed_at"]
    assert db.run_record["accounts_seen"] == 0
