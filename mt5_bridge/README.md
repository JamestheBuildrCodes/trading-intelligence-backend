# Tradvoryx MT5 Python Bridge (read-only)

This Windows sidecar connects an already logged-in MetaTrader 5 terminal to the Tradvoryx backend. It reads account metrics and historical deals only. It does not contain trade execution functions and must not be represented as a native MT5 EA.

## Requirements and demo-first workflow

1. Use Windows with a compatible 64-bit Python installation and the MetaTrader 5 desktop terminal.
2. Open MT5 and log in to the intended **demo account**. Confirm history is available.
3. Create an account in Tradvoryx.
4. Using a valid Supabase user access token, call `POST /api/v1/accounts/{account_id}/connector-tokens`. The raw token is returned once only. Save it securely; never put it in Git.
5. In this directory, run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

6. Edit `.env` with the Render API URL, account UUID and one-time token. Set `MT5_TERMINAL_PATH` if MT5 isn't discoverable.
7. Run `python mt5_bridge.py`. Start with a demo account and reconcile its imported history against the MT5 terminal before relying on analytics.
8. This bridge requires a logged-in MT5 desktop terminal and a running bridge process. To sync when your personal PC is off, run the bridge on a reliable hosted Windows VPS. A fully managed cloud connection requires a separate provider integration, account provisioning and server-side credentials; that is not delivered by this local bridge.

## Reliability notes

- Initial history lookback defaults to 30 days; later runs use an overlapping window. Trade deals and MT5 balance operations are sent in the same sync. Cash movements are de-duplicated by account + MT5 deal ticket, and daily equity/balance snapshots are refreshed on successful syncs.\n- Positive MT5 balance operations are classified as deposits and negative ones as withdrawals. Some brokers use balance operations for corrections or bonuses too; review imported entries and correct the classification manually where needed. Do not manually enter a movement already imported.
- The bridge requires a logged-in terminal. It does not validate the broker/account identity against dashboard metadata yet; verify the connected login, broker server and account currency in the logs and reconcile them manually before relying on the data.
- The current schema does not faithfully represent netting reversals (`DEAL_ENTRY_INOUT`); such positions are skipped by trade aggregation to avoid misleading metrics. Full netting support is a required follow-up.
- The deployed backend must have the current schema and environment variables configured before running this bridge.
- Connector tokens are account-scoped. Revoke and reissue any token that may have been exposed.
- This is analytics infrastructure, not financial advice and not a guarantee of future results.
