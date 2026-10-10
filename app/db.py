from functools import lru_cache

from supabase import create_client, Client

from app.config import get_settings


@lru_cache
def get_supabase() -> Client:
    """
    Elevated server client — bypasses RLS entirely. This is intentional:
    FastAPI is the trusted layer for account writes and MT5 ingestion.
    Every account-scoped endpoint MUST still verify the requester owns the
    account because RLS does not enforce that check for this server client.
    """
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_admin_key)
