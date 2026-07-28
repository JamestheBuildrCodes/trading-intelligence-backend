from functools import lru_cache
from supabase import create_client, Client

from app.config import get_settings


@lru_cache
def get_supabase() -> Client:
    """
    Service-role client — bypasses RLS entirely. This is intentional: the
    backend is the trusted layer that RLS is defending *against* client
    access, not a client itself. Every function that uses this MUST verify
    the requesting user actually owns the account_id/user_id it touches —
    RLS will not do that check for you here.
    """
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
