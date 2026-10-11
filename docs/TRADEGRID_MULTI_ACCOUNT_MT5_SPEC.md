# TradeGrid Backend Expansion — Multi-Account MT5 and Full-History Analytics

This is the implementation contract for a commercial multi-tenant trading journal. The document itself is not feature completion.

## Domain model
Adapt existing schema rather than duplicating concepts:
- trading_accounts: id, user_id, display_name, account_type (live/demo/prop_evaluation/prop_funded/other), broker, server, platform, currency, initial_balance, timezone, prop_firm, challenge_phase, metadata, created_at, archived_at.
- account_connections: account_id, adapter/provider, status, encrypted_secret_reference (never raw credentials), last_sync_at, last_success_at, history_start/end, sync_cursor/checkpoint, last_error_code.
- deals/orders/positions: source identifiers and provenance; normalized deals/fills; partial closes, commissions, swaps, fees and balance operations. Unique keys make imports idempotent per account + source system + source identifier.
- sync_jobs/import_runs: range, cursor, status, fetched/inserted/updated/skipped counts, error summary, start/end, idempotency key.
- account_risk_rules and prop_rule_snapshots: configurable values, source, effective date, timezone and evaluation evidence.
- execution_costs and trade_annotations: measured/estimated provenance, currency, category, reference/fill prices, slippage method, mistake tags, notes and timestamps.
- alerts and audit_events: user-scoped, explainable trigger/evidence, no secrets.

Follow current database/migration conventions. Enforce ownership on every API and row-level policy. Never store MT5 passwords as plaintext. Use a secret manager/KMS or documented encrypted store with key separation, redaction and rotation.

## API behavior
- CRUD/archive accounts; create connection intent; verify connector; request initial import/resync; fetch status/coverage; disconnect/revoke.
- Trade history supports cursor pagination and filters, but analytics endpoints must not cap at 200 trades. Return earliest/latest available timestamps and completeness.
- Analytics accept account_ids, date range, symbol, direction, strategy/tag and timezone; aggregate the full filtered dataset in SQL or streaming/server-side aggregation.
- Risk endpoints return open exposures, concentration/correlation warnings, per-account and valid portfolio drawdown, and explainable alert evidence.
- Prop endpoints configure rules and calculate buffers only when inputs exist.
- Cost endpoints separate recorded costs from estimates and return method/sample size.
- Exports enforce ownership and use bounded background jobs for large datasets.
- All endpoints authenticate, validate ownership and payloads, rate-limit expensive work, return consistent errors and never reveal secrets.

## Full-history correctness
No silent LIMIT 200 or equivalent in any metric path. UI list pagination is fine; analytics use every eligible record. Use date filters, indexed predicates, DB aggregation or chunked streaming. Treat deposits/withdrawals separately from trading P&L. Normalize timezones and currencies. Missing MAE/MFE, spread, slippage or unrealized P&L means unknown, not zero. Document formulas.

## Hosted MT5
A browser cannot connect directly to arbitrary MT5 servers. Define an adapter and long-running hosted worker/queue in an environment that supports the selected MT5 bridge. Validate feasibility for target brokers before claiming support. Workers need per-account scheduling, leases/locks, retries/backoff, rate limits, health checks, reconnects, cursor checkpoints, initial backfill, incremental sync and safe shutdown. If the bridge requires Windows/terminal binaries, provision isolated compatible workers; do not pretend a Linux-only web process is enough.

Read-only journal scope; no trade execution. Minimize credential collection, document permissions and handling.

## Correlated risk
Use configurable symbol/asset/currency mappings and record correlation provenance. Distinguish statistical return correlation from intuitive related-instrument exposure. Never claim a precise correlation without price series, lookback, interval and sample size. For open positions, use broker snapshots and contract metadata; missing inputs mean unknown. Never aggregate unlike currencies without timestamped FX conversion.

## Tests and release gates
Fixtures must cover >200 trades, multi-account isolation, duplicate imports, partial closes, swaps/commission, balance adjustments, timezone/currency differences, missing quotes, resync/retry and prop drawdown edge cases. Test P&L/expectancy/drawdown formulas, migrations, API auth and ownership. Staging must verify connect → full history import → reconcile totals → incremental sync → analytics → export before production.

Document infrastructure, environment variables, queue/worker deployment, secret storage, monitoring, cost estimates and rollback. If external MT5 bridge, quote feed, secret manager, billing or notification credentials are unavailable, document the blocker and mark the feature unconfigured.
