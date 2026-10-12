from functools import lru_cache

import httpx
from supabase import create_client, Client
from supabase.lib.client_options import SyncClientOptions

from app.config import get_settings


@lru_cache
def get_supabase() -> Client:
    """
    Elevated server client — bypasses RLS entirely. This is intentional:
    FastAPI is the trusted layer for account writes and MT5 ingestion.
    Every account-scoped endpoint MUST still verify the requester owns the
    account because RLS does not enforce that check for this server client.

    Use a bounded, long-lived HTTP/1.1 client. Render has repeatedly observed
    transient HTTP/2 ReadError/EAGAIN failures on Supabase REST requests; a
    dedicated pooled client avoids creating a new transport per request and
    disables HTTP/2 multiplexing for this service.
    """
    settings = get_settings()
    http_client = httpx.Client(
        http2=False,
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=5,
            keepalive_expiry=15.0,
        ),
    )
    options = SyncClientOptions(httpx_client=http_client)
    return create_client(settings.supabase_url, settings.supabase_admin_key, options=options)
