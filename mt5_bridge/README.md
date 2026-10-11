# Trading Intelligence MT5 Python Bridge (read-only)

This Windows sidecar connects an already logged-in MetaTrader 5 terminal to the Trading Intelligence backend. It reads account metrics and historical deals only. It does not contain trade execution functions and must not be represented as a native MT5 EA.

## Requirements and demo-first workflow

1. Use Windows with a compatible 64-bit Python installation and the MetaTrader 5 desktop terminal.
2. Open MT5 and log in to the intended **demo account**. Confirm history is available.
3. Create an account in Trading Intelligence.
4. Using a valid Supabase user access token, call `POST /api/v1/accounts/{account_id}/connector-tokens`. The raw token is returned once only. Save it securely; never put it in Git.
5. In this directory, run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

6. Edit `.env` with the Render API URL, account UUID and one-time token. Set `MT5_TERMINAL_PATH` if MT5 isn't discoverable.
7. Run `python mt5_bridge.py`. Start with a demo account and compare its imported history with the MT5 terminal before using the numbers for decisions.
8. For always-on sync with this bridge, MT5 and the process must run on a reliable Windows VPS/host. The current bridge is **not** a cloud connection that runs after a trader shuts down every terminal. A hosted-provider integration (for example, MetaApi) requires a provider account, server-side API credentials, account provisioning and cost/security review.

## Reliability notes

- Initial history backfill defaults to 10,000 days and is split into 90-day windows; configure `MT5_INITIAL_LOOKBACK_DAYS` and `MT5_HISTORY_CHUNK_DAYS` in `.env` if needed. This reaches well before typical MT5 account history, but the actual range is still bounded by broker/terminal history availability; set the terminal history filter to show all available history and reconcile the range.
- The bridge persists a non-secret local checkpoint in `.tradegrid_sync_state.json` after each fully uploaded date window, then uses a two-day overlap to catch late changes. Keep this file on durable private storage. If it is lost, the bridge performs the long initial backfill again.
- Backend uniqueness on account + MT5 deal ID is intended to make retries idempotent.
- The bridge requires a logged-in terminal. It does not yet verify that the terminal login, broker server and currency match the dashboard profile automatically; verify them and reconcile the imported totals manually.
- The current schema does not faithfully represent netting reversals (`DEAL_ENTRY_INOUT`); such positions are skipped by trade aggregation to avoid misleading metrics. Full netting support is a required follow-up.
- The deployed backend must have the current schema and environment variables configured before running this bridge.
- Connector tokens are account-scoped. Revoke and reissue any token that may have been exposed.
- This is analytics infrastructure, not financial advice and not a guarantee of future results.
