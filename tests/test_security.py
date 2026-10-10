from app.security import generate_connector_token, hash_connector_token


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
