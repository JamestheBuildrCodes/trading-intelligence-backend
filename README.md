# Trading Intelligence — Backend (FastAPI)

FastAPI backend for the Trading Intelligence dashboard.

## Implemented

- Supabase Auth validation for dashboard bearer tokens.
- Account creation/listing and account-ownership checks on account-scoped API calls.
- One-time, account-scoped connector tokens; only hashes are stored.
- Read-only Windows MT5 Python bridge under `mt5_bridge/`.
- Connector ingestion of raw MT5 deal history, duplicate-safe by account + deal ID.
- Position aggregation for common open/close and partial-close histories.
- Overview metrics and paginated closed-trade listing.\n- Daily balance/equity snapshots and cash-flow-adjusted portfolio growth tracking.\n- User-recorded deposits and withdrawals with Modified Dietz return calculation.
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
