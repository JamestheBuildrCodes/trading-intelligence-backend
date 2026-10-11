# Trading Intelligence — Backend (FastAPI)

FastAPI backend for the Trading Intelligence dashboard.

## Implemented

- Supabase Auth validation for dashboard bearer tokens.
- Account creation/listing and account-ownership checks on account-scoped API calls.
- One-time, account-scoped connector tokens; only hashes are stored.
- Read-only Windows MT5 Python bridge under `mt5_bridge/`.
- Connector ingestion of raw MT5 deal history, duplicate-safe by account + deal ID.
- Position aggregation for common open/close and partial-close histories.
- Overview metrics and paginated closed-trade listing.\n- Daily balance/equity snapshots and cash-flow-adjusted portfolio growth tracking.\n- MT5 balance-operation import plus manual cash-flow entries, with Modified Dietz return calculation.
- Health endpoint at `/health`.

## Known limitations

- `inout` netting reversal deals are stored but excluded from current position aggregation until position-leg support is implemented.
- Expectancy in R is an approximation based on configured fixed-currency risk, not broker-reported initial stop risk.
- Sessions are coarse UTC-hour classifications and are not daylight-saving-aware.
- Rules & Risk, Trading DNA, Performance Leaks, Experiments, AI Coach and Telegram are not complete backend products yet.
- A native MT5 Expert Advisor (MQL5) is not present. The current bridge is a Python sidecar running on Windows next to MT5.

## Local setup

1. Run `database/schema.sql` in Supabase SQL Editor for a new project. For the existing Trading Intelligence project, use the incremental migration in `database/migrations/20261011_align_backend_schema_and_auth_profile_trigger.sql` instead; do not replace the existing expanded schema with the initial schema.
2. Copy `.env.example` to `.env` and fill in every required value.
3. Create and activate a virtual environment, then install dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

4. Start the API with `uvicorn app.main:app --reload`.
5. Check `/health` and `/docs`.

Never commit `.env`. Keep `SUPABASE_SECRET_KEY` (or legacy `SUPABASE_SERVICE_ROLE_KEY`) server-only and never expose it to the browser. The service-role client bypasses RLS, so every user-facing account-scoped endpoint must enforce account ownership.

## Environment variables

- `SUPABASE_URL`: Supabase Project URL.
- `SUPABASE_SECRET_KEY`: recommended server-only secret key.
- `SUPABASE_SERVICE_ROLE_KEY`: optional legacy fallback.
- `CONNECTOR_TOKEN_PEPPER`: random secret of at least 32 characters. Keep stable after issuing connector tokens.
- `ALLOWED_ORIGINS`: comma-separated frontend origins, e.g. `http://localhost:3000,https://your-app.vercel.app`.
- `AI_DAILY_BUDGET_USD`: reserved for future AI integrations.

Generate the connector pepper locally with:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

## Deploying on Render

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Set `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `CONNECTOR_TOKEN_PEPPER`, and `ALLOWED_ORIGINS` in Render.
- Set `ALLOWED_ORIGINS` to exact deployed frontend origins (scheme + host, no path).
- Add the production frontend domain to Supabase Auth URL Configuration.
- Confirm the deployment is live and `/health` returns `{"status":"ok"}`.

See [docs/supabase-setup.md](docs/supabase-setup.md) for dashboard configuration and smoke tests.

## MT5 demo connection

See [mt5_bridge/README.md](mt5_bridge/README.md). Connect a demo account first. The bridge only reads account information and deal history; it cannot submit, modify or close trades. Verify totals against MT5 history before relying on analytics.

## Hosted MT5 connections (MetaApi)

TradeGrid can provision an MT5 account on MetaApi and run read-only history/position synchronization from a separate worker. The account form accepts an **investor/read-only password only**. TradeGrid does not persist that password, but the credential is forwarded to MetaApi, which must retain it to maintain the hosted terminal. Do not connect accounts you are not authorized to monitor.

### Required production setup
1. Create a MetaApi account, review its pricing/region/data-processing terms, and generate an API token with only the access needed for account provisioning and RPC history reads.
2. Add the token as the server-only Render secret `METAAPI_TOKEN` on the web API service. Never add it as a `NEXT_PUBLIC_` variable or commit it.
3. Apply `database/migrations/20261011_add_hosted_mt5_connection.sql` to Supabase.
4. Deploy a separate Render Cron Job from this repository/commit with command `python -m app.workers.hosted_mt5_sync`, environment `METAAPI_TOKEN`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY` (or supported legacy service-role key), and `CONNECTOR_TOKEN_PEPPER`. Test job runtime and timeout with actual history volumes.
5. Start with an authorized broker demo account. Verify login, server, currency, earliest/latest deals, trade counts, deposits/withdrawals and open positions against MT5 before using this for decisions or marketing hosted support.

Endpoints:
- `POST /api/v1/accounts/{account_id}/hosted-mt5` provisions a hosted account.
- `GET /api/v1/accounts/{account_id}/hosted-mt5/status` reports connection state and imported timestamps.
- `POST /api/v1/accounts/{account_id}/hosted-mt5/disconnect` requests provider deprovisioning.

History sync uses `METAAPI_INITIAL_LOOKBACK_DAYS` (default 10,000), `METAAPI_HISTORY_CHUNK_DAYS` (default 180), and `METAAPI_MAX_HISTORY_CHUNKS_PER_RUN` (default 10). The worker recursively subdivides ranges when a MetaApi request reaches the 1,000-deal response cap. If a one-second range is still saturated, it raises `history_response_limit_reached` and does not advance the cursor past that interval. A completed configured lookback does not prove the broker retained all history; reconcile imported history with the broker statement.

The worker is a separate process and must not run inside the web request handler. If `METAAPI_TOKEN` is not configured, hosted provisioning remains disabled. The existing Windows MT5 bridge remains available as an alternative.

## Paystack subscription billing (gated until merchant setup)

TradeGrid includes a server-side Paystack billing flow. Paid checkout remains disabled until the merchant configures the provider secret and the exact plan codes in Render. No browser-supplied amount grants access.

### Paystack dashboard configuration
Create four recurring plans in **USD** with these exact amounts and intervals:
- TradeGrid Trader Monthly — USD 15, monthly.
- TradeGrid Trader Annual — USD 144, annually.
- TradeGrid Pro Monthly — USD 29, monthly.
- TradeGrid Pro Annual — USD 288, annually.

Set the resulting plan codes as `PAYSTACK_TRADER_MONTHLY_PLAN_CODE`, `PAYSTACK_TRADER_YEARLY_PLAN_CODE`, `PAYSTACK_PRO_MONTHLY_PLAN_CODE`, and `PAYSTACK_PRO_YEARLY_PLAN_CODE`. Set `PAYSTACK_SECRET_KEY` server-side, and set `APP_BASE_URL` to the real production URL. Confirm the Paystack account is enabled for USD billing and recurring card subscriptions before turning checkout on.

Configure the Paystack webhook URL to:
`https://trading-intelligence-backend.onrender.com/api/v1/billing/webhook`

The webhook verifies Paystack's HMAC-SHA512 signature, verifies each successful transaction server-side, checks its reference/amount/currency/plan against a TradeGrid-created checkout intent, processes provider event IDs idempotently, and never activates an entitlement from a browser return alone. Subsequent recurring charges are linked to an existing subscription and checked against the server-configured plan amount. Failed payments move the subscription to past_due; verified cancellation events remove paid entitlements.

### Endpoints
- `GET /api/v1/billing/status` — current server-owned plan and entitlement state.
- `POST /api/v1/billing/checkout` with `{ "plan": "trader" | "pro", "billing_period": "monthly" | "yearly" }` — creates a provider checkout session only if billing is configured.
- `POST /api/v1/billing/cancel` — requests recurring subscription cancellation through Paystack.
- `POST /api/v1/billing/webhook` — Paystack event receiver.

Apply `database/migrations/20261011_add_paystack_billing.sql` before deploying the billing-enabled backend. The migration creates server-only subscription, checkout-intent and webhook-idempotency tables with row-level security enabled. The current enforcement gate covers account count and premium analytics modules; before accepting payment, exercise the complete first-payment, recurring-payment, failure and cancellation lifecycle with Paystack test mode and ensure plan codes/amounts match.
