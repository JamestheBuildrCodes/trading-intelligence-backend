from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import entitlements


class Query:
    def select(self, _columns): return self
    def eq(self, _column, _value): return self
    def limit(self, _limit): return self
    def execute(self): return SimpleNamespace(data=[{"id": "acct", "user_id": "user"}])


class DB:
    def table(self, _name): return Query()


def test_beta_flag_temporarily_allows_hosted_mt5_for_free_users(monkeypatch):
    monkeypatch.setattr(entitlements, "get_settings", lambda: SimpleNamespace(hosted_mt5_beta_enabled=True))
    monkeypatch.setattr(entitlements, "get_user_entitlements", lambda *_: {
        "plan": "free", "entitlements": dict(entitlements.PLAN_ENTITLEMENTS["free"])
    })
    result = entitlements.require_account_feature(DB(), "acct", "hosted_mt5")
    assert result["plan"] == "free"


def test_hosted_mt5_stays_plan_gated_when_beta_flag_is_off(monkeypatch):
    monkeypatch.setattr(entitlements, "get_settings", lambda: SimpleNamespace(hosted_mt5_beta_enabled=False))
    monkeypatch.setattr(entitlements, "get_user_entitlements", lambda *_: {
        "plan": "free", "entitlements": dict(entitlements.PLAN_ENTITLEMENTS["free"])
    })
    with pytest.raises(HTTPException) as exc:
        entitlements.require_account_feature(DB(), "acct", "hosted_mt5")
    assert exc.value.status_code == 403
