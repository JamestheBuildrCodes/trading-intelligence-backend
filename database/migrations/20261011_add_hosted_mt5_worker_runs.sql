-- Track the hosted-MT5 scheduled worker so the API never treats a token
-- alone as proof that cloud synchronization is operational.
create table if not exists public.hosted_mt5_worker_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  status text not null default 'running'
    check (status in ('running', 'succeeded', 'partial', 'failed')),
  accounts_seen integer not null default 0 check (accounts_seen >= 0),
  accounts_succeeded integer not null default 0 check (accounts_succeeded >= 0),
  accounts_failed integer not null default 0 check (accounts_failed >= 0),
  safe_error_code text,
  created_at timestamptz not null default now()
);
create index if not exists hosted_mt5_worker_runs_latest_idx
  on public.hosted_mt5_worker_runs(started_at desc);

alter table public.hosted_mt5_worker_runs enable row level security;
-- Service-role worker writes and backend health checks read this table. No
-- browser policies are created.
