-- Backend-only operational tables must not be directly accessible through the Supabase Data API.
-- FastAPI uses the service-role client and enforces user/account ownership itself.
REVOKE ALL PRIVILEGES ON TABLE
  public.billing_checkout_intents,
  public.billing_webhook_events,
  public.connector_tokens,
  public.hosted_mt5_worker_runs,
  public.mt5_open_positions,
  public.mt5_position_snapshots,
  public.portfolio_cash_flows,
  public.portfolio_snapshots,
  public.user_subscriptions
FROM anon, authenticated;

-- Keep future tables backend-only by default; grant access explicitly when a table
-- is intentionally designed for direct client access.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE ALL ON TABLES FROM anon, authenticated;
