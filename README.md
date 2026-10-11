# Tradvoryx — Backend (FastAPI)

FastAPI backend for the Tradvoryx dashboard.

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

1. Run `database/schema.sql` in Supabase SQL Editor for a new project. For the existing Tradvoryx project, use the incremental migration in `database/migrations/20261011_align_backend_schema_and_auth_profile_trigger.sql` instead; do not replace the existing expanded schema with the initial schema.
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

## Hosted MT5 connections and free GitHub Actions history import

Tradvoryx already has one read-only MetaApi history worker. Use the repository's GitHub-hosted runner instead of paying for a separate Render Cron Job. The account must first be configured through the existing hosted connection flow; GitHub Actions does not store or receive the MT5 password.

### Database readiness verified

The current production Supabase project has the hosted connection columns (connection_provider, provider_account_id, hosted_connection_status, history cursor/completeness and status timestamps), the live position snapshot tables, and the worker-run health table. The audited schema also has:
- raw_deals unique on (account_id, mt5_deal_id);
- trades unique on (account_id, mt5_position_id);
- portfolio_cash_flows unique on (account_id, external_id);
- foreign keys from these records to their owning account, with current-position snapshots linked to the account.

The migration database/migrations/20261011_add_hosted_mt5_connection.sql exists and its required columns/constraints are present in production. The worker health table is also present. No destructive migration is required for this workflow.

### Required GitHub Actions repository secrets

In GitHub, open **trading-intelligence-backend → Settings → Secrets and variables → Actions → New repository secret** and add:

- METAAPI_TOKEN — MetaApi server-side token that can access the hosted MT5 account.
- SUPABASE_URL — https://znjxpvhjivtdtuitxqxo.supabase.co.
- SUPABASE_SECRET_KEY — server-only Supabase secret/service-role key accepted by the backend. The code also supports legacy SUPABASE_SERVICE_ROLE_KEY, but this workflow expects SUPABASE_SECRET_KEY.
- CONNECTOR_TOKEN_PEPPER — the **existing stable** pepper value used by the backend, at least 32 characters. Copy the same value; do not rotate it for this workflow.

The GitHub integration cannot read or create Actions secret values. Do not paste the values into chat or commit them. Copy the existing server-side values from the trusted secret manager/Render configuration where available.

### First manual run

1. In Tradvoryx **Accounts**, open the existing Headway account whose server is Headway-Real. Connect it through the hosted MT5 flow using an investor/read-only password. The broker login/server/currency must match this Tradvoryx account. The password is submitted to MetaApi for cloud hosting; Tradvoryx does not persist it. Do not use a master trading password.
2. Confirm the account's hosted provider connection is saved and its status is not disconnected. The importer refuses to process the target unless the submitted account UUID points to a configured MetaApi provider account, includes a login/server, and identifies Headway REAL/LIVE (not demo).
3. Open the [backend repository Actions tab](https://github.com/JamestheBuildrCodes/trading-intelligence-backend/actions).
4. Select **Hosted MT5 historical import (manual)** and choose **Run workflow**.
5. Enter the exact UUID of the new Headway-Real row from Supabase public.trading_accounts. The latest production database check found only one demo account and no Headway live profile, so create the real profile first; do not select the demo row.
6. Tick the explicit confirmation that this is the Headway real/live account. Choose 10 chunks for the first run, then start the workflow.

The workflow runs Python 3.11, installs requirements.txt, runs the backend pytest suite, checks the worker CLI, then invokes the existing worker as follows (the actual UUID and chunk count are passed from workflow inputs):

    python -m app.workers.hosted_mt5_sync --account-id "$TARGET_ACCOUNT_ID" --max-history-chunks "$MAX_HISTORY_CHUNKS"

Each chunk covers at most 180 days. The workflow default is 10 chunks (about 1,800 days of range per run); choose 20 for a larger bounded batch. The job timeout is 45 minutes and GitHub Actions concurrency prevents overlapping workflow runs for the same account. Scheduling is disabled in the committed workflow. After the initial import has been verified, the commented cron example can be enabled in .github/workflows/hosted-mt5-sync.yml and adjusted to your desired interval.

### Progress, completion and safe reruns

Open the workflow run and expand the import step logs. It reports:
- target account reference (only the final eight UUID characters), broker/server, and the number of chunks;
- each UTC date window started and the durable cursor saved;
- provider rows, normalized trading deals, balance cash-flow operations, unsupported/unrecognized rows, IDs already present, and newly-seen deal-ID estimates;
- bounded database batch progress, position aggregation progress, and earliest/latest observed deal timestamps.

A run may finish successfully while hosted_initial_backfill_complete=false; in that case it exits successfully for that bounded batch, records backfill_incomplete_more_runs_required, and the next manual run continues from hosted_history_cursor_at. Repeat runs until the account status confirms the configured lookback is scanned. A successful scan means only that the configured time range was queried; it does not prove that Headway/MetaApi retains every lifetime deal.

The database uses stable MT5 deal IDs and account-scoped unique constraints. Inserts are upserted with duplicate-ignore semantics; existing deals are not overwritten, and a failed database write prevents the history cursor from advancing for that date window. Provider 1,000-row responses are recursively split into smaller disjoint time ranges; a still-saturated one-second range fails closed. Retry after a failure is safe because deals/cash flows are idempotent and checkpoints only advance after a window's writes complete.

Raw deal history and position-based reconstructed trade rows are distinct. The worker does **not** reconstruct historical balance/equity curves from deals: the account/equity snapshots represent sync-time observations. Partial-close and unsupported netting-reversal records have limits in the current aggregate model, so reconcile trade/deal counts and costs against an MT5 statement before relying on analytics. Report the earliest/latest imported timestamps and any provider retention restrictions.

### Failure conditions

The workflow exits nonzero when required secrets are missing/invalid, the UUID does not exist, the account is not configured with a hosted MetaApi account ID, the target is demo/non-Headway/non-live/disconnected, provider/database sync fails after retry, or the worker cannot save its health record. A missing account is not treated as a successful no-op.

The Actions runner uses the same configured secrets to connect directly to MetaApi and Supabase. It does not call Render and does not require a Render Cron Job. GitHub-hosted runner quotas and MetaApi API/subscription limits still apply.

## Paystack subscription billing (gated until merchant setup)

Tradvoryx includes a server-side Paystack billing flow. Paid checkout remains disabled until the merchant configures the provider secret and the exact plan codes in Render. No browser-supplied amount grants access.

### Paystack dashboard configuration
Create four recurring plans in **USD** with these exact amounts and intervals. A Nigerian Paystack merchant must have USD acceptance enabled (and an eligible USD settlement arrangement) before setting up these plans; account eligibility for international payments and recurring card charges is determined by Paystack, not Tradvoryx.
- Tradvoryx Trader Monthly — USD 15, monthly.
- Tradvoryx Trader Annual — USD 144, annually.
- Tradvoryx Pro Monthly — USD 29, monthly.
- Tradvoryx Pro Annual — USD 288, annually.

Set the resulting plan codes as `PAYSTACK_TRADER_MONTHLY_PLAN_CODE`, `PAYSTACK_TRADER_YEARLY_PLAN_CODE`, `PAYSTACK_PRO_MONTHLY_PLAN_CODE`, and `PAYSTACK_PRO_YEARLY_PLAN_CODE`. Set `PAYSTACK_SECRET_KEY` server-side, and set `APP_BASE_URL` to the real production URL. Confirm the Paystack account is enabled for USD billing and recurring card subscriptions before turning checkout on.

Configure the Paystack webhook URL to:
`https://trading-intelligence-backend.onrender.com/api/v1/billing/webhook`

Checkout requires a verified Supabase email. The backend enforces the account cap in both FastAPI and an atomic PostgreSQL trigger, so concurrent account-creation requests cannot bypass a plan allowance.

The webhook verifies Paystack's HMAC-SHA512 signature, verifies each successful transaction server-side, checks its reference/amount/currency/plan against a Tradvoryx-created checkout intent, processes provider event IDs idempotently, and never activates an entitlement from a browser return alone. Subsequent recurring charges are linked to an existing subscription and checked against the server-configured plan amount. Failed payments move the subscription to past_due; verified cancellation events remove paid entitlements.

### Endpoints
- `GET /api/v1/billing/status` — current server-owned plan and entitlement state.
- `POST /api/v1/billing/checkout` with `{ "plan": "trader" | "pro", "billing_period": "monthly" | "yearly" }` — creates a provider checkout session only if billing is configured.
- `POST /api/v1/billing/cancel` — requests recurring subscription cancellation through Paystack.
- `POST /api/v1/billing/webhook` — Paystack event receiver.

Apply `database/migrations/20261011_add_paystack_billing.sql` before deploying the billing-enabled backend. The migration creates server-only subscription, checkout-intent and webhook-idempotency tables with row-level security enabled. The current enforcement gate covers account count and premium analytics modules; before accepting payment, exercise the complete first-payment, recurring-payment, failure and cancellation lifecycle with Paystack test mode and ensure plan codes/amounts match.
