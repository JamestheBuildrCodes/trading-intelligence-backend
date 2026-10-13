from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.routers.hosted_mt5 import _worker_health


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows
    def select(self, _columns):
        return self
    def eq(self, _column, _value):
        return self
    def order(self, _column, desc=False):
        return self
    def limit(self, _count):
        return self
    def execute(self):
        return SimpleNamespace(data=self.rows)


class FakeDB:
    def __init__(self, rows=None, should_raise=False):
        self.rows = rows or []
        self.should_raise = should_raise
    def table(self, _name):
        if self.should_raise:
            raise RuntimeError("worker table missing")
        return FakeQuery(self.rows)


def test_hosted_worker_requires_recent_completed_success():
    now = datetime.now(timezone.utc)
    fresh = FakeDB([{
        "started_at": now.isoformat(),
        "completed_at": (now - timedelta(seconds=30)).isoformat(),
        "status": "succeeded",
        "accounts_seen": 0,
        "accounts_succeeded": 0,
        "accounts_failed": 0,
        "safe_error_code": None,
    }])
    result = _worker_health(fresh, 'c5688758-a78a-482e-aae7-ceb782c0984a')
    assert result["worker_ready"] is True
    assert result["worker_status"] == "succeeded"


def test_partial_worker_run_is_ready_only_inside_health_window():
    now = datetime.now(timezone.utc)
    partial = FakeDB([{
        "started_at": now.isoformat(),
        "completed_at": (now - timedelta(seconds=60)).isoformat(),
        "status": "partial",
        "accounts_seen": 2,
        "accounts_succeeded": 1,
        "accounts_failed": 1,
        "safe_error_code": "one_or_more_accounts_failed",
    }])
    result = _worker_health(partial, 'c5688758-a78a-482e-aae7-ceb782c0984a')
    assert result["worker_ready"] is True
    assert result["worker_status"] == "partial"


def test_stale_or_missing_worker_run_is_not_ready():
    now = datetime.now(timezone.utc)
    stale = FakeDB([{
        "started_at": (now - timedelta(hours=1)).isoformat(),
        "completed_at": (now - timedelta(minutes=16)).isoformat(),
        "status": "succeeded",
        "accounts_seen": 0,
        "accounts_succeeded": 0,
        "accounts_failed": 0,
        "safe_error_code": None,
    }])
    assert _worker_health(stale, 'c5688758-a78a-482e-aae7-ceb782c0984a')["worker_ready"] is False
    assert _worker_health(FakeDB([]), 'c5688758-a78a-482e-aae7-ceb782c0984a')["worker_status"] == "never_ran"


def test_missing_worker_health_table_fails_closed():
    result = _worker_health(FakeDB(should_raise=True), 'c5688758-a78a-482e-aae7-ceb782c0984a')
    assert result["worker_ready"] is False
    assert result["worker_status"] == "health_unavailable"
