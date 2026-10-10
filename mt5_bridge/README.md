# TradeGrid MT5 Bridge (read-only)

This bridge runs on **Windows**, on the same computer/VPS as MetaTrader 5. It uses the official MetaTrader5 Python package to read account information and deal history, then sends closed/opening deal records to the TradeGrid backend. It does not submit, change, or close orders.

## Important limitation

This bridge is a first integration path, not proof that the full product is production-ready. It requires a working MT5 terminal, a logged-in account, a deployed backend, the database schema/migrations applied, and a connector token. It has not been validated against your specific broker terminal from this repository environment. Test with a demo account first.

## Setup on Windows

1. Install 64-bit Python compatible with the installed MetaTrader 5 terminal and install MetaTrader 5.
2. Open MT5, log in to the intended **demo account first**, and confirm quotes/history are available.
3. Create a TradeGrid account in the dashboard, or use the existing account UUID.
4. Using a valid Supabase access token, call `POST /api/v1/accounts/{account_id}/connector-tokens`. The response contains a token displayed only once. Keep it private.
5. Download this repository and open PowerShell in `mt5_bridge`.
6. Create a virtual environment and install dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

7. Edit `.env` with the backend URL, account UUID, and connector token. Never commit this file.
8. If MT5 is installed in a nonstandard location, set `MT5_TERMINAL_PATH` to the full path of `terminal64.exe`. Otherwise leave it blank and keep MT5 open and logged in.
9. Run:

```powershell
python mt5_bridge.py
```

The first run reads up to the configured lookback (30 days by default); later polls re-read a two-day overlap. Duplicate deal uploads are intended to be safe because the backend upserts on account + MT5 deal ID.

## Safety and operational notes

- The bridge is read-only and contains no order execution calls.
- Use a separate connector token for each TradeGrid account. Revoke it if exposed.
- Keep the MT5 terminal and bridge process running for ongoing sync.
- A desktop computer sleeping or a VPS disconnecting will stop sync.
- Backend authentication, schema, broker symbol conventions, netting/hedging behaviour, commission handling, and history completeness must be verified before relying on analytics.
- This initial implementation uses the opening deal to determine position direction. If the configured history window does not contain an opening deal for a position, that deal is skipped rather than guessing direction.
- Do not use these analytics as a guarantee of future trading performance.
