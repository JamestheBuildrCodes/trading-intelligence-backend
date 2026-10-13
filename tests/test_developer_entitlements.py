from types import SimpleNamespace

from app.services.entitlements import PLAN_ENTITLEMENTS, get_user_entitlements


class FakeQuery:
    def __init__(self, row):
        self.row = row

    def select(self, _columns):
        return self

    def eq(self, _column, _value):
        return self

    def limit(self, _limit):
        return self

    def execute(self):
        return SimpleNamespace(data=[self.row] if self.row else [])


class FakeDB:
    def __init__(self, row):
        self.row = row

    def table(self, _name):
        return FakeQuery(self.row)


def test_developer_entitlement_unlocks_enterprise_without_paid_subscription(monkeypatch):
    monkeypatch.setattr('app.services.entitlements.get_settings', lambda: SimpleNamespace(beta_free_all_features=False))
    result = get_user_entitlements(FakeDB({
        "plan": "free",
        "status": "inactive",
        "developer_access": True,
        "developer_preview_plan": "enterprise",
        "current_period_end": None,
    }), "owner-user-id")

    assert result["developer_access"] is True
    assert result["plan"] == "enterprise"
    assert result["paid"] is False
    assert result["subscription_status"] == "developer"
    assert result["entitlements"] == PLAN_ENTITLEMENTS["enterprise"]


def test_developer_preview_obeys_selected_customer_tier(monkeypatch):
    monkeypatch.setattr('app.services.entitlements.get_settings', lambda: SimpleNamespace(beta_free_all_features=False))
    result = get_user_entitlements(FakeDB({
        "plan": "free",
        "status": "inactive",
        "developer_access": True,
        "developer_preview_plan": "free",
        "current_period_end": None,
    }), "owner-user-id")

    assert result["developer_access"] is True
    assert result["plan"] == "free"
    assert result["entitlements"] == PLAN_ENTITLEMENTS["free"]


def test_non_developer_cannot_receive_developer_entitlement_by_default(monkeypatch):
    monkeypatch.setattr('app.services.entitlements.get_settings', lambda: SimpleNamespace(beta_free_all_features=False))
    result = get_user_entitlements(FakeDB({
        "plan": "free",
        "status": "inactive",
        "developer_access": False,
        "developer_preview_plan": "enterprise",
        "current_period_end": None,
    }), "ordinary-user-id")

    assert result["developer_access"] is False
    assert result["plan"] == "free"
    assert result["entitlements"] == PLAN_ENTITLEMENTS["free"]



def test_free_beta_unlocks_all_features_for_free_accounts(monkeypatch):
    monkeypatch.setattr('app.services.entitlements.get_settings', lambda: SimpleNamespace(beta_free_all_features=True))
    result = get_user_entitlements(FakeDB({
        "plan": "free",
        "status": "inactive",
        "developer_access": False,
        "current_period_end": None,
    }), "beta-tester-id")

    assert result["plan"] == "free"
    assert result["paid"] is False
    assert result["beta_access"] is True
    assert result["entitlements"] == PLAN_ENTITLEMENTS["enterprise"]
    assert result["entitlements"]["max_accounts"] is None
    assert result["entitlements"]["hosted_mt5"] is True
    assert result["entitlements"]["portfolio_risk"] is True
