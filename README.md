# Trading Intelligence — Backend (FastAPI)

FastAPI backend for the Trading Intelligence dashboard.

## Implemented today

- Supabase Auth validation for dashboard bearer tokens.
- Account creation/listing and account-ownership checks on account-scoped API calls.
- One-time, account-scoped connector tokens; only hashed tokens are stored.
- Read-only Windows MT5 Python bridge under `mt5_bridge/`.
- Connector ingestion of raw MT5 deal history, duplicate-safe by account + deal ID.
- Position aggregation for common open/close and partial-close histories.
- Overview metrics and paginated closed-trade listing.

## Known limitations — do not market these as complete

- `inout` netting reversal deals are stored but deliberately excluded from current position aggregation to avoid creating false combined trades. A position-leg schema and tests are still needed for full netting support.
- Expectancy in R is an approximation based on configured fixed-currency risk, not broker-reported initial stop risk.
- Sessions are coarse UTC-hour classifications; they are not DST-aware trading-session analytics.
- Rules & Risk, Trading DNA, Performance Leaks, Experiments, AI Coach and Telegram are not complete backend products yet.
- A native MT5 Expert Advisor (MQL5) is not present. The current bridge is a Python sidecar running on Windows next to the MT5 terminal.

## Local setup

1. Create a Supabase project.
2. Run `database/schema.sql` in **Supabase Dashboard → SQL Editor**.
3. Copy `.env.example` to `.env` and set every required value.
4. Create and activate a virtual environment, then install dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

5. Start the API with `uvicorn app.main:app --reload`.
6. Check `/health` and `/docs`.

Never commit `.env`. Keep `SUPABASE_SERVICE_ROLE_KEY` server-only and never expose it to the browser. Because the service-role client bypasses RLS, every user-facing account-scoped endpoint must enforce ownership.

## Environment variables

- `SUPABASE_URL` — Supabase Project URL.
- `SUPABASE_SERVICE_ROLE_KEY` — server-only service-role key.
- `CONNECTOR_TOKEN_PEPPER` — strong random secret. Keep it stable after connector tokens are issued; rotating it invalidates existing connector tokens.
- `ALLOWED_ORIGINS` — comma-separated allowed frontend origins.
- `AI_DAILY_BUDGET_USD` — reserved budget setting for a future AI integration.

## Deploying on Render

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Set the server-only environment variables in Render.
- Set `ALLOWED_ORIGINS` to the exact deployed frontend origin(s).
- Add the production frontend domain to Supabase Auth URL Configuration.

## MT5 demo connection

See [mt5_bridge/README.md](mt5_bridge/README.md). Connect a demo account first. The bridge only reads account information and deal history; it cannot submit, modify or close trades. Verify totals against MT5 history before relying on analytics.
