from pydantic import ValidationError
import pytest

from app.schemas import HostedMt5ConnectIn


def test_hosted_mt5_requires_explicit_cloud_credential_acknowledgement():
    valid = HostedMt5ConnectIn(
        login="123456",
        server="Broker-Demo",
        investor_password="read-only-example",
        acknowledge_cloud_storage=True,
    )
    assert valid.login == "123456"
    assert valid.server == "Broker-Demo"

    with pytest.raises(ValidationError):
        HostedMt5ConnectIn(
            login="123456",
            server="Broker-Demo",
            investor_password="read-only-example",
            acknowledge_cloud_storage=False,
        )


def test_hosted_mt5_trims_login_and_server():
    valid = HostedMt5ConnectIn(
        login=" 123456 ",
        server=" Broker-Demo ",
        investor_password="read-only-example",
        acknowledge_cloud_storage=True,
    )
    assert valid.login == "123456"
    assert valid.server == "Broker-Demo"
