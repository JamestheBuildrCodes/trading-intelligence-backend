# Trading Intelligence — Backend (FastAPI)

FastAPI backend for the TradeGrid/Trading Intelligence dashboard.

## What's built

- `POST /api/v1/accounts` and `GET /api/v1/accounts` — create/list a user's trading accounts.
- `POST /api/v1/accounts/{id}/connector-tokens` — issue a one-time read-only MT5 bridge token.
- `POST /api/v1/connector/deals` — ingest closed MT5 deals through a separate connector-token trust boundary.
- `GET /api/v1/accounts/{id}/overview` and `GET /api/v1/accounts/{id}/trades` — dashboard performance endpoints.
- Deal-to-trade aggregation from `raw_deals` into `trades`, with idempotent deal ingestion.
- Supabase Auth validation for dashboard access tokens. Tokens are verified by Supabase Auth itself, so the backend does not depend on a legacy HS256 signing secret.
- A read-only Windows MT5 Python bridge under `mt5_bridge/`.

## Not built yet

Rules engine violations, the full Trading DNA/Performance Leaks/Experiments analysis suite, AI Coach, Telegram bot, and a full-featured native MT5 EA. The current API should not be described as having these features complete.

## Local setup

1. Create a Supabase project.
2. Run the full `database/schema.sql` file in **Supabase Dashboard -> SQL Editor**.
3. Create a local environment file from the example and fill in the values:
   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. Start the API:
   ```bash
   uvicorn app.main:app --reload
   ```
5. Visit `http://localhost:8000/health` for a successful health response and `http://localhost:8000/docs` for the API reference.

Do not commit `.env`; do not put the service-role key in the frontend. The server-only key bypasses RLS, so every account-scoped API endpoint must retain the user/account ownership checks.

## Environment variables

- `SUPABASE_URL`: Supabase Project URL.
- `SUPABASE_SERVICE_ROLE_KEY`: server-only service-role key.
- `CONNECTOR_TOKEN_PEPPER`: a long random secret used to hash MT5 connector tokens. Keep it stable once issued.
- `ALLOWED_ORIGINS`: comma-separated frontend origins, for example `http://localhost:3000,https://your-app.vercel.app`.
- `AI_DAILY_BUDGET_USD`: future AI-call budget limit.

See [the Supabase setup guide](docs/supabase-setup.md) for dashboard configuration, auth redirects, deployment, and smoke testing.

## Deploying (Render)

1. Connect this repository as a Web Service.
2. Build command: `pip install -r requirements.txt`.
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. Add the backend environment variables in Render's Environment tab.
5. Add the full production frontend origin to `ALLOWED_ORIGINS`, then deploy.
6. Add the production frontend URL to Supabase Authentication -> URL Configuration.

Render free services may sleep after inactivity, so the first request can be slow.

## Safety notes

The MT5 bridge is read-only: it does not place, modify, or close trades. Test with a demo account first and reconcile the received deal history with the broker terminal. The connector token is returned only once; revoke and issue another if it is lost.
