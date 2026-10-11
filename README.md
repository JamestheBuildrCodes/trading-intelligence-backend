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
1. Create a MetaApi account, review its current pricing/region/data-processing terms, and generate an API token with only the access needed for account provisioning and RPC history reads.
2. Add the token as the server-side Render secret `METAAPI_TOKEN` on the web API service. Never add it as a `NEXT_PUBLIC_` environment variable, commit it, or paste it into this repository.
3. Apply `database/migrations/20261011_add_hosted_mt5_connection.sql` to Supabase.
4. Deploy a separate Render Cron Job from this repository/commit with command `python -m app.workers.hosted_mt5_sync`, environment `METAAPI_TOKEN`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY` (or the already supported service-role key), and `CONNECTOR_TOKEN_PEPPER`. Schedule it frequently enough to satisfy the product's sync expectations; test its run duration and timeout with actual history volumes.
5. Start with a broker demo account. Verify the connected login, exact server, account currency, earliest/latest deal timestamps, record counts, cash-flow treatment, and open position snapshots against the broker terminal before enabling marketing claims or paid access.

The API endpoints are:
- `POST /api/v1/accounts/{account_id}/hosted-mt5` to provision a hosted account.
- `GET /api/v1/accounts/{account_id}/hosted-mt5/status` to inspect state and imported-history timestamps.
- `POST /api/v1/accounts/{account_id}/hosted-mt5/disconnect` to request provider undeploy and removal.

Hosted history sync is bounded by `METAAPI_INITIAL_LOOKBACK_DAYS` (default 10,000 days), `METAAPI_HISTORY_CHUNK_DAYS` (default 180), and `METAAPI_MAX_HISTORY_CHUNKS_PER_RUN` (default 10). Checkpoints advance only after a history window is persisted. The cursor completing means TradeGrid scanned its configured window; it **does not prove the broker has retained all historical records**. Reconcile imported history against the broker statement. MetaApi stores and operates the remote terminal; provider availability and costs are external dependencies.

The worker is deliberately a separate process: do not run it inside the serverless frontend or an unbounded FastAPI request. In environments with no configured `METAAPI_TOKEN` the provision endpoint returns a clear disabled response; account records do not pretend to be connected.
