import hashlib
import hmac
from types import SimpleNamespace

from app.routers.billing import CURRENCY, PLANS, _signed_webhook
from app.services.entitlements import PLAN_ENTITLEMENTS, get_user_entitlements


def test_paystack_webhook_signature_requires_valid_sha512_hmac():
    raw = b'{"event":"charge.success","data":{"reference":"tg_demo"}}'
    secret = "test-secret-key"
    valid = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()
    assert _signed_webhook(raw, valid, secret)
    assert _signed_webhook(raw, valid.upper(), secret)
    assert not _signed_webhook(raw + b" ", valid, secret)
    assert not _signed_webhook(raw, None, secret)


def test_paystack_plan_amounts_and_intervals_match_pricing_offer():
    assert CURRENCY == "USD"
    assert PLANS[("trader", "monthly")]["amount"] == 1500
    assert PLANS[("trader", "yearly")]["amount"] == 14400
    assert PLANS[("pro", "monthly")]["amount"] == 2900
    assert PLANS[("pro", "yearly")]["amount"] == 28800
    assert all(item.get("setting", "").startswith("paystack_") for item in PLANS.values())


def test_subscription_missing_defaults_to_free_entitlements(monkeypatch):
    monkeypatch.setenv('BETA_FREE_ALL_FEATURES', 'false')
    class Query:
        def select(self, _columns):
            return self
        def eq(self, _column, _value):
            return self
        def limit(self, _limit):
            return self
        def execute(self):
            return SimpleNamespace(data=[])

    class DB:
        def table(self, _name):
            return Query()

    result = get_user_entitlements(DB(), "user-1")
    assert result["plan"] == "free"
    assert result["paid"] is False
    assert result["entitlements"]["max_accounts"] == 1
    assert result["entitlements"]["hosted_mt5"] is False


def test_cancelled_or_past_due_subscription_does_not_grant_paid_entitlements(monkeypatch):
    monkeypatch.setenv('BETA_FREE_ALL_FEATURES', 'false')
    class Query:
        def select(self, _columns):
            return self
        def eq(self, _column, _value):
            return self
        def limit(self, _limit):
            return self
        def execute(self):
            return SimpleNamespace(data=[{
                "plan": "pro",
                "status": "past_due",
                "billing_period": "monthly",
                "current_period_end": None,
                "cancel_at_period_end": False,
            }])

    class DB:
        def table(self, _name):
            return Query()

    result = get_user_entitlements(DB(), "user-1")
    assert result["plan"] == "free"
    assert result["paid"] is False
    assert result["entitlements"] == PLAN_ENTITLEMENTS["free"]

from datetime import datetime, timedelta, timezone


def test_billing_period_clamps_end_of_month_safely():
    from app.routers.billing import _add_billing_period
    assert _add_billing_period(datetime(2026, 1, 31, tzinfo=timezone.utc), "monthly") == datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert _add_billing_period(datetime(2028, 2, 29, tzinfo=timezone.utc), "yearly") == datetime(2029, 2, 28, tzinfo=timezone.utc)


def test_active_subscription_without_valid_period_end_does_not_grant_paid_access(monkeypatch):
    monkeypatch.setenv('BETA_FREE_ALL_FEATURES', 'false')
    class Query:
        def select(self, _columns): return self
        def eq(self, _column, _value): return self
        def limit(self, _limit): return self
        def execute(self):
            return SimpleNamespace(data=[{
                "plan": "pro",
                "status": "active",
                "billing_period": "monthly",
                "current_period_end": None,
                "cancel_at_period_end": False,
            }])
    class DB:
        def table(self, _name): return Query()
    result = get_user_entitlements(DB(), "user-1")
    assert result["plan"] == "free"
    assert result["paid"] is False
