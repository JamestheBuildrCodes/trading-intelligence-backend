-- TradeGrid live-position risk snapshots and configurable prop-rule metadata.
-- Additive migration: does not alter or delete existing account/trade data.

alter table public.trading_accounts
  add column if not exists prop_firm text,
  add column if not exists challenge_phase text;

alter table public.prop_firm_rules
  add column if not exists daily_drawdown_mode text not null default 'percentage',
  add column if not exists max_drawdown_mode text not null default 'percentage',
  add column if not exists max_drawdown_type text not null default 'static',
  add column if not exists profit_target_mode text not null default 'percentage',
  add column if not exists max_lot_size numeric(20, 8),
  add column if not exists rules_source text not null default 'user_configured',
  add column if not exists rules_verified_at timestamptz,
  add column if not exists rules_timezone text not null default 'UTC',
  add column if not exists notes text not null default '';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'prop_firm_rules_daily_mode_check' and conrelid = 'public.prop_firm_rules'::regclass) then
    alter table public.prop_firm_rules add constraint prop_firm_rules_daily_mode_check check (daily_drawdown_mode in ('percentage', 'absolute'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'prop_firm_rules_max_mode_check' and conrelid = 'public.prop_firm_rules'::regclass) then
    alter table public.prop_firm_rules add constraint prop_firm_rules_max_mode_check check (max_drawdown_mode in ('percentage', 'absolute'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'prop_firm_rules_max_type_check' and conrelid = 'public.prop_firm_rules'::regclass) then
    alter table public.prop_firm_rules add constraint prop_firm_rules_max_type_check check (max_drawdown_type in ('static', 'trailing'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'prop_firm_rules_target_mode_check' and conrelid = 'public.prop_firm_rules'::regclass) then
    alter table public.prop_firm_rules add constraint prop_firm_rules_target_mode_check check (profit_target_mode in ('percentage', 'absolute'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'prop_firm_rules_max_lot_size_check' and conrelid = 'public.prop_firm_rules'::regclass) then
    alter table public.prop_firm_rules add constraint prop_firm_rules_max_lot_size_check check (max_lot_size is null or max_lot_size > 0);
  end if;
end $$;

create table if not exists public.mt5_position_snapshots (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references public.trading_accounts(id) on delete cascade,
  snapshot_at timestamptz not null,
  currency text not null,
  balance numeric(20, 4),
  equity numeric(20, 4),
  is_complete boolean not null default false,
  created_at timestamptz not null default now()
);
create index if not exists mt5_position_snapshots_latest_idx
  on public.mt5_position_snapshots(account_id, snapshot_at desc)
  where is_complete = true;

create table if not exists public.mt5_open_positions (
  id uuid primary key default gen_random_uuid(),
  snapshot_id uuid not null references public.mt5_position_snapshots(id) on delete cascade,
  account_id uuid not null references public.trading_accounts(id) on delete cascade,
  mt5_position_id text not null,
  symbol text not null,
  direction text not null check (direction in ('buy', 'sell')),
  volume numeric(20, 8) not null check (volume > 0),
  open_price numeric(20, 10) not null check (open_price >= 0),
  current_price numeric(20, 10) not null check (current_price >= 0),
  stop_loss numeric(20, 10),
  take_profit numeric(20, 10),
  unrealized_profit numeric(20, 4) not null default 0,
  swap numeric(20, 4) not null default 0,
  risk_to_stop numeric(20, 4),
  opened_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint mt5_open_positions_snapshot_position_unique unique (snapshot_id, mt5_position_id),
  constraint mt5_open_positions_stop_risk_check check (risk_to_stop is null or risk_to_stop >= 0)
);
create index if not exists mt5_open_positions_snapshot_idx on public.mt5_open_positions(snapshot_id);
create index if not exists mt5_open_positions_account_symbol_idx on public.mt5_open_positions(account_id, symbol);

alter table public.mt5_position_snapshots enable row level security;
alter table public.mt5_open_positions enable row level security;
-- No browser policies: writes/reads are mediated by FastAPI ownership or connector-token checks.
