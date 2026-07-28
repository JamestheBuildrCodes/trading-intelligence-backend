# Trading Intelligence — Backend (FastAPI)

## What's built
- `POST /api/v1/accounts` / `GET /api/v1/accounts` — create/list your trading accounts
- `POST /api/v1/accounts/{id}/connector-tokens` — issue a bearer token for the MT5 bridge
- `POST /api/v1/connector/deals` — ingestion endpoint the MT5 bridge pushes closed deals to
- `GET /api/v1/accounts/{id}/overview` — the numbers the dashboard's KPI cards need
- `GET /api/v1/accounts/{id}/trades` — trade list with symbol/session filters
- Deal-to-trade aggregation (raw_deals → trades), idempotent, handles partial closes
- Supabase Auth verification (dashboard requests) + separate connector-token auth (MT5 bridge requests) — two different trust boundaries, see docs/ADR.md

## Not built yet
- Rules engine violations, Trading DNA, Performance Leaks, Experiments, AI Coach, Telegram bot
- The MT5 EA/bridge itself (next piece — this backend is what it talks to)

## Local setup
```
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the values below
uvicorn app.main:app --reload
```
Visit `http://localhost:8000/health` — should return `{"status": "ok"}`.
Interactive API docs at `http://localhost:8000/docs`.

## Environment variables — where each one comes from
- `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` — Supabase dashboard →
  Project Settings → API. Service role key is secret — never put it in the
  frontend or commit it.
- `SUPABASE_JWT_SECRET` — same page, "JWT Settings" section.
- `CONNECTOR_TOKEN_PEPPER` — any long random string you make up yourself,
  e.g. from `python3 -c "import secrets; print(secrets.token_hex(32))"`.
- `ALLOWED_ORIGINS` — your Vercel frontend URL(s), comma-separated.

## Deploying (Render free tier)
1. Push this `backend/` folder's repo to GitHub (same account as the frontend, can be the same repo or a separate one — separate is simpler for two different deploy targets).
2. render.com → New → Web Service → connect the repo.
3. Root directory: `backend` (if it's in the same repo as the frontend).
4. Build command: `pip install -r requirements.txt`
5. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
6. Add the environment variables above under the service's Environment tab.
7. Deploy. You'll get a URL like `https://your-service.onrender.com`.
8. Update the frontend's `NEXT_PUBLIC_API_BASE_URL` env var in Vercel to that URL, and add that same URL to `ALLOWED_ORIGINS` here (redeploy both).

Free-tier Render services sleep after inactivity — the first request after
a sleep takes ~30-50s to wake up. This matters for the MT5 bridge and any
scheduled job hitting this API; a keep-alive ping (e.g. the GitHub Action
already stubbed in `.github/workflows/daily-report.yml`) can hide this.

## Database
Apply `database/schema.sql` in the Supabase SQL Editor before running any
of this — every endpoint here assumes those tables and RLS policies exist.
