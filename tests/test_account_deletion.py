from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers.accounts import delete_account


class FakeQuery:
    def __init__(self, db, table):
        self.db = db
        self.table_name = table
        self.action = "select"
        self.filters = {}

    def select(self, *_args):
        self.action = "select"
        return self

    def delete(self):
        self.action = "delete"
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def limit(self, _count):
        return self

    def execute(self):
        if self.action == "select":
            return SimpleNamespace(data=self.db.account)
        self.db.deleted_id = self.filters.get("id")
        return SimpleNamespace(data=[])


class FakeDatabase:
    def __init__(self, account):
        self.account = [account] if account else []
        self.deleted_id = None

    def table(self, name):
        assert name == "trading_accounts"
        return FakeQuery(self, name)


def test_delete_account_removes_unhosted_account():
    db = FakeDatabase({
        "id": "account-123",
        "connection_provider": None,
        "provider_account_id": None,
    })

    result = delete_account(account_id="account-123", db=db)

    assert result == {"account_id": "account-123", "deleted": True}
    assert db.deleted_id == "account-123"


def test_delete_account_requires_hosted_connection_to_be_disconnected_first():
    db = FakeDatabase({
        "id": "account-123",
        "connection_provider": "metaapi",
        "provider_account_id": "provider-123",
    })

    with pytest.raises(HTTPException) as exc:
        delete_account(account_id="account-123", db=db)

    assert exc.value.status_code == 409
    assert db.deleted_id is None


def test_delete_account_returns_404_for_missing_account():
    db = FakeDatabase(None)

    with pytest.raises(HTTPException) as exc:
        delete_account(account_id="missing", db=db)

    assert exc.value.status_code == 404
    assert db.deleted_id is None
