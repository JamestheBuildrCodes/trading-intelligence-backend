from app.schemas import HostedMt5ConnectIn


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
