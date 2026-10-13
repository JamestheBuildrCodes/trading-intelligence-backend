from app.schemas import HostedMt5ConnectIn
from app.routers.hosted_mt5 import _safe_provider_failure


def _connect_input(**updates):
    data = {
        "login": "12345678",
        "server": "ExampleBroker-Real",
        "investor_password": "read-only-secret",
        "acknowledge_cloud_storage": True,
    }
    data.update(updates)
    return data


def test_hosted_connection_defaults_to_automatic_provider_fallback():
    request = HostedMt5ConnectIn(**_connect_input())
    assert request.provider == "auto"


def test_hosted_connection_accepts_explicit_provider_choice():
    assert HostedMt5ConnectIn(**_connect_input(provider="tickerall")).provider == "tickerall"
    assert HostedMt5ConnectIn(**_connect_input(provider="metaapi")).provider == "metaapi"


def test_hosted_connection_rejects_unknown_provider():
    import pytest

    with pytest.raises(ValueError):
        HostedMt5ConnectIn(**_connect_input(provider="unknown-provider"))



def test_safe_provider_failure_preserves_actionable_safe_details():
    failure = _safe_provider_failure(
        "tickerall",
        {
            "code": "FREE_TIER_LIVE_REJECTED",
            "message": "TickerAll's current plan does not permit this live account through the API.",
            "raw": "should never be returned",
        },
    )
    assert failure == {
        "provider": "tickerall",
        "code": "FREE_TIER_LIVE_REJECTED",
        "message": "TickerAll's current plan does not permit this live account through the API.",
    }
    assert "raw" not in failure


def test_safe_provider_failure_bounds_unexpected_details():
    failure = _safe_provider_failure("metaapi", "x" * 1000)
    assert failure["provider"] == "metaapi"
    assert failure["code"] == "metaapi_rejected"
    assert len(failure["message"]) == 500
