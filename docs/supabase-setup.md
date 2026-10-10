# Supabase setup for Trading Intelligence

This is a one-time setup. You do not need to write application code or send credentials to anyone.

## 1. Create the Supabase project

1. Sign in at https://supabase.com/dashboard and create a project.
2. Save the database password somewhere private; the app does not need it for normal operation.
3. Wait until the project is ready.

## 2. Create the database tables

1. Open your project -> SQL Editor -> New query.
2. Open database/schema.sql in this repository, copy the entire file and paste it into the SQL Editor.
3. Click Run.
4. Check Table Editor; you should see trading_accounts, connector_tokens, raw_deals, and trades.

The script is designed to be re-runnable for the initial schema. Browser-side authenticated users can read only their own account/deal/trade records; account writes and balance/sync-status updates must go through FastAPI. If you previously ran an earlier version of this schema that created account write policies, also run `database/migrations/20261010_lock_down_account_writes.sql` in the SQL Editor. For later schema changes, use a new migration instead of editing production data by hand.

## 3. Configure email authentication and redirect URLs

In Supabase, open Authentication -> URL Configuration:

- Site URL (local development): http://localhost:3000
- Redirect URLs: add http://localhost:3000/**
- Once deployed, also add your exact production origin, for example https://your-app.vercel.app/**. Replace the example with your real Vercel/custom domain.

Open Authentication -> Sign In / Providers -> Email and make sure Email is enabled. For safer real-user onboarding, leave email confirmation on. During local testing only, you can disable confirmation to avoid needing to configure SMTP; do not assume production email will be delivered through the default test mail service.

## 4. Add the frontend environment variables

In the frontend repository, copy .env.example to .env.local and fill these values from Project Settings -> API:

    NEXT_PUBLIC_SUPABASE_URL=https://YOUR-PROJECT.supabase.co
    NEXT_PUBLIC_SUPABASE_ANON_KEY=YOUR_SUPABASE_PUBLISHABLE_OR_ANON_KEY
    NEXT_PUBLIC_API_BASE_URL=http://localhost:8000

The URL and publishable/anon key are designed for browser use. Never put the service-role key in a NEXT_PUBLIC_* variable.

## 5. Configure the backend environment

Copy the backend .env.example to .env for local development. Fill in:

- SUPABASE_URL: the same Project URL.
- SUPABASE_SERVICE_ROLE_KEY: the server-only service-role key from Project Settings -> API Keys (use the legacy service_role key if your dashboard separates legacy JWT keys and newer publishable/secret API keys).
- CONNECTOR_TOKEN_PEPPER: generate using python -c "import secrets; print(secrets.token_hex(32))". Keep it stable after you create connector tokens.
- ALLOWED_ORIGINS: your exact frontend origins, comma-separated, with no trailing slash.

The backend now validates dashboard access tokens by calling Supabase Auth. It does not require or store the project's JWT signing secret.

## 6. Deploy the backend

In Render, open the backend Web Service -> Environment and add the same backend values as above. Do not upload the .env file to GitHub. Deploy with:

- Build command: pip install -r requirements.txt
- Start command: uvicorn app.main:app --host 0.0.0.0 --port $PORT

After deploying, open https://YOUR-BACKEND.onrender.com/health. A successful response is {"status":"ok"}.

## 7. Deploy the frontend

In Vercel, open the frontend project -> Settings -> Environment Variables and add:

- NEXT_PUBLIC_SUPABASE_URL
- NEXT_PUBLIC_SUPABASE_ANON_KEY
- NEXT_PUBLIC_API_BASE_URL = your deployed backend URL

Set these for Production (and Preview if you use preview deployments), then redeploy. Add the Vercel domain to Supabase redirect URLs and to the backend ALLOWED_ORIGINS.

## 8. First login smoke test

1. Open the frontend URL and create an account with an email address you can access.
2. If email confirmation is on, open the confirmation email and click the link.
3. You should land in onboarding. Create a trading account using a demo account first.
4. Confirm the account appears in the Accounts page and Overview opens without an authentication error.

The MT5 bridge remains read-only. Do not use a live account until the demo sync has been reconciled against MT5 history and the database rows.

## Common problems

- Invalid or expired session token: check that frontend and backend use the same Supabase project URL, then sign out and sign back in.
- Redirect URL not allowed: add the exact origin/path pattern under Authentication -> URL Configuration.
- CORS error: ensure the full frontend origin (scheme + hostname, no path) is in backend ALLOWED_ORIGINS.
- Backend returns 500 or table not found: verify database/schema.sql has been run against the same Supabase project used in the backend environment.
- Signup succeeds but no email arrives: configure SMTP for production, check spam, and review Supabase Auth logs.
