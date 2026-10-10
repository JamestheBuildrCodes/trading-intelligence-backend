from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.deps import get_current_user


class FakeAuth:
    def __init__(self, user=None, error=None):
        self.user = user
        self.error = error
        self.received_token = None

    def get_user(self, token):
        self.received_token = token
        if self.error:
            raise self.error
        return SimpleNamespace(user=self.user)


class FakeSupabase:
    def __init__(self, auth):
        self.auth = auth


def test_get_current_user_asks_supabase_to_validate_token():
    fake_auth = FakeAuth(user=SimpleNamespace(id="user-123", email="trader@example.com"))
    user = get_current_user(authorization="Bearer valid-token", db=FakeSupabase(fake_auth))

    assert fake_auth.received_token == "valid-token"
    assert user.id == "user-123"
    assert user.email == "trader@example.com"


@pytest.mark.parametrize("authorization", [None, "", "Basic not-a-bearer", "Bearer "])
def test_get_current_user_rejects_missing_or_wrong_scheme(authorization):
    with pytest.raises(HTTPException) as exc:
        get_current_user(authorization=authorization, db=FakeSupabase(FakeAuth()))
    assert exc.value.status_code == 401


def test_get_current_user_hides_supabase_auth_errors():
    fake_auth = FakeAuth(error=RuntimeError("upstream internals"))
    with pytest.raises(HTTPException) as exc:
        get_current_user(authorization="Bearer expired-token", db=FakeSupabase(fake_auth))
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid or expired session token"
