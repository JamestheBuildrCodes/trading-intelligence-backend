-- Portfolio growth tracking: daily MT5 balance/equity snapshots and user-recorded cash flows.
-- Apply in Supabase SQL Editor after the existing migrations. This is additive.
create table if not exists public.portfolio_snapshots (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references public.trading_accounts(id) on delete cascade,
    snapshot_date date not null,
    captured_at timestamptz not null default now(),
    balance numeric(20, 4) not null check (balance >= 0),
    equity numeric(20, 4) not null check (equity >= 0),
    currency text not null,
    source text not null default 'mt5_connector' check (source in ('mt5_connector', 'manual')),
    created_at timestamptz not null default now(),
    constraint portfolio_snapshots_account_date_unique unique (account_id, snapshot_date)
);
create index if not exists portfolio_snapshots_account_date_idx on public.portfolio_snapshots(account_id, snapshot_date);
create table if not exists public.portfolio_cash_flows (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references public.trading_accounts(id) on delete cascade,
    flow_type text not null check (flow_type in ('deposit', 'withdrawal')),
    amount numeric(20, 4) not null check (amount > 0),
    occurred_at timestamptz not null,
    note text not null default '' check (length(note) <= 500),
    created_by uuid references auth.users(id) on delete set null,
    created_at timestamptz not null default now()
);
create index if not exists portfolio_cash_flows_account_time_idx on public.portfolio_cash_flows(account_id, occurred_at);
alter table public.portfolio_snapshots enable row level security;
alter table public.portfolio_cash_flows enable row level security;
-- Reads/writes go through FastAPI, which enforces account ownership. No browser policies are added.

-- Idempotent additions for broker-imported cash movements.
alter table public.portfolio_cash_flows
  add column if not exists source text not null default 'manual';
alter table public.portfolio_cash_flows
  add column if not exists external_id text;
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.portfolio_cash_flows'::regclass
      and conname = 'portfolio_cash_flows_source_check'
  ) then
    alter table public.portfolio_cash_flows
      add constraint portfolio_cash_flows_source_check check (source in ('manual', 'mt5'));
  end if;
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.portfolio_cash_flows'::regclass
      and conname = 'portfolio_cash_flows_account_external_unique'
  ) then
    alter table public.portfolio_cash_flows
      add constraint portfolio_cash_flows_account_external_unique unique (account_id, external_id);
  end if;
end $$;
