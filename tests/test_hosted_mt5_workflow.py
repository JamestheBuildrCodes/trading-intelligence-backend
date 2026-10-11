from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.workers import hosted_mt5_sync as worker


LIVE_ACCOUNT = {
    "id": "c5688758-a78a-482e-aae7-ceb782c0984a",
    "account_type": "personal",
    "broker": "Headway",
    "server": "Headway-Real",
    "mt5_login": "12345678",
    "connection_provider": "metaapi",
    "provider_account_id": "provider-account-id",
    "hosted_connection_status": "connected",
}


def test_workflow_is_manual_and_requires_live_confirmation_and_secrets():
    path = Path(__file__).parents[1] / ".github" / "workflows" / "hosted-mt5-sync.yml"
    text = path.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "confirm_live_headway_account:" in text
    assert "default: false" in text
    assert "METAAPI_TOKEN: ${{ secrets.METAAPI_TOKEN }}" in text
    assert "SUPABASE_URL: ${{ secrets.SUPABASE_URL }}" in text
    assert "SUPABASE_SECRET_KEY: ${{ secrets.SUPABASE_SECRET_KEY }}" in text
    assert "CONNECTOR_TOKEN_PEPPER: ${{ secrets.CONNECTOR_TOKEN_PEPPER }}" in text
    assert "python -m app.workers.hosted_mt5_sync" in text
    # The schedule is a commented example only, not an active trigger.
    assert "\n# schedule:" in text
    assert "\non:\n  schedule:" not in text


def test_worker_requires_one_explicitly_selected_account():
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 2


def test_headway_live_account_is_eligible():
    worker._validate_headway_live_account(LIVE_ACCOUNT, LIVE_ACCOUNT["id"])


@pytest.mark.parametrize(
    ("patch", "error"),
    [
        ({"connection_provider": None}, "target_account_not_hosted"),
        ({"provider_account_id": None}, "provider_account_id_missing"),
        ({"hosted_connection_status": "disconnected"}, "target_account_disconnected"),
        ({"broker": "Another Broker", "server": "OtherBroker-Live"}, "target_account_is_not_headway"),
        ({"server": "Headway-Demo"}, "demo_accounts_are_not_allowed"),
        ({"server": "Headway-Contest"}, "target_account_is_not_live"),
        ({"account_type": "demo"}, "demo_accounts_are_not_allowed"),
    ],
)
def test_headway_live_account_validation_rejects_unsafe_targets(patch, error):
    row = dict(LIVE_ACCOUNT)
    row.update(patch)
    with pytest.raises(ValueError, match=error):
        worker._validate_headway_live_account(row, LIVE_ACCOUNT["id"])


def test_headway_live_account_id_must_match_explicit_dispatch_target():
    with pytest.raises(ValueError, match="target_account_id_mismatch"):
        worker._validate_headway_live_account(LIVE_ACCOUNT, "11111111-1111-1111-1111-111111111111")


def test_missing_secrets_fail_without_initializing_database(monkeypatch):
    for name in (
        "METAAPI_TOKEN",
        "SUPABASE_URL",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "CONNECTOR_TOKEN_PEPPER",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="missing_required_configuration"):
        asyncio.run(worker.run_once(LIVE_ACCOUNT["id"], max_chunks=1))


class FakeQuery:
    def __init__(self, db):
        self.db = db
        self.operation = "select"
        self.payload = None
        self.ids = []
        self.account_id = None

    def select(self, _fields):
        self.operation = "select"
        return self

    def eq(self, field, value):
        if field == "account_id":
            self.account_id = value
        return self

    def in_(self, _field, values):
        self.ids = list(values)
        return self

    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        self.operation = "upsert"
        self.payload = list(rows)
        self.db.upserts.append({
            "rows": list(rows),
            "on_conflict": on_conflict,
            "ignore_duplicates": ignore_duplicates,
        })
        return self

    def execute(self):
        if self.operation == "select":
            existing = [
                {"mt5_deal_id": item}
                for item in self.ids if (self.account_id, item) in self.db.existing
            ]
            return SimpleNamespace(data=existing)
        if self.db.fail_upsert:
            raise RuntimeError("simulated database write error")
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, existing=(), fail_upsert=False):
        self.existing = set(existing)
        self.fail_upsert = fail_upsert
        self.upserts = []

    def table(self, _name):
        return FakeQuery(self)


def _deal(deal_id: str, position_id: str = "position-1"):
    return {
        "mt5_deal_id": deal_id,
        "mt5_position_id": position_id,
        "entry_type": "in",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.1,
        "price": 1.1,
        "profit": 0,
        "commission": 0,
        "swap": 0,
        "deal_time": "2026-10-01T10:00:00+00:00",
    }


def test_upload_deals_deduplicates_inputs_and_does_not_overwrite_existing(monkeypatch):
    db = FakeDB(existing={(LIVE_ACCOUNT["id"], "already-imported")})
    aggregated = []
    monkeypatch.setattr(worker, "aggregate_position", lambda _db, account, position: aggregated.append((account, position)))
    result = worker._upload_deals(
        db,
        LIVE_ACCOUNT["id"],
        [_deal("new-deal"), _deal("already-imported"), _deal("new-deal")],
    )
    assert result["unique_deals"] == 2
    assert result["duplicate_input_ids"] == 1
    assert result["already_present"] == 1
    assert result["new_id_estimate"] == 1
    assert db.upserts[0]["on_conflict"] == "account_id,mt5_deal_id"
    assert db.upserts[0]["ignore_duplicates"] is True
    assert len(db.upserts[0]["rows"]) == 2
    assert aggregated == [(LIVE_ACCOUNT["id"], "position-1")]


def test_upload_deals_uses_bounded_batches(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(worker, "aggregate_position", lambda *_args: None)
    deals = [_deal(f"deal-{i}", f"position-{i}") for i in range(1201)]
    result = worker._upload_deals(db, LIVE_ACCOUNT["id"], deals)
    assert [len(batch["rows"]) for batch in db.upserts] == [500, 500, 201]
    assert result["unique_deals"] == 1201
    assert result["new_id_estimate"] == 1201


def test_database_write_errors_are_not_silently_swallowed(monkeypatch):
    db = FakeDB(fail_upsert=True)
    monkeypatch.setattr(worker, "aggregate_position", lambda *_args: None)
    with pytest.raises(RuntimeError, match="simulated database write error"):
        worker._upload_deals(db, LIVE_ACCOUNT["id"], [_deal("new-deal")])
