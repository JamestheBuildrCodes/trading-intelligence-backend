import pytest

from app.config import get_settings
from app.security import generate_connector_token, hash_connector_token


@pytest.fixture(autouse=True)
def configured_test_settings(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-server-secret-key")
    monkeypatch.setenv("CONNECTOR_TOKEN_PEPPER", "test-pepper-" + "x" * 40)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_connector_token_is_not_stored_as_plaintext():
    raw_token, token_hash = generate_connector_token()
    assert raw_token
    assert raw_token != token_hash
    assert hash_connector_token(raw_token) == token_hash


def test_generated_connector_tokens_are_unique():
    first, first_hash = generate_connector_token()
    second, second_hash = generate_connector_token()
    assert first != second
    assert first_hash != second_hash
