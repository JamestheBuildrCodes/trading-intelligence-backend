-- Track hosted worker health per Tradvoryx account, so one tester's failed sync
-- does not mark every other tester's connection unhealthy.
alter table public.hosted_mt5_worker_runs
  add column if not exists account_id uuid references public.trading_accounts(id) on delete set null;

create index if not exists hosted_mt5_worker_runs_account_latest_idx
  on public.hosted_mt5_worker_runs(account_id, started_at desc);
