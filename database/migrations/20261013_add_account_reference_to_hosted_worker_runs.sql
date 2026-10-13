-- Store the target account for each hosted-MT5 worker run.
-- Historical runs remain valid with a NULL account_id.
ALTER TABLE public.hosted_mt5_worker_runs
  ADD COLUMN IF NOT EXISTS account_id uuid
  REFERENCES public.trading_accounts(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS hosted_mt5_worker_runs_account_started_idx
  ON public.hosted_mt5_worker_runs (account_id, started_at DESC);
