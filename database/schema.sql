-- Trading Intelligence initial schema. Run in Supabase SQL Editor.
-- API service-role access bypasses RLS; app/deps.py enforces API account ownership.
create extension if not exists pgcrypto;

create table if not exists public.trading_accounts (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    label text not null check (length(trim(label)) between 1 and 100),
    broker text,
    server text,
    mt5_login text,
    currency text not null default 'USD',
    account_type text not null default 'personal',
    initial_balance numeric(20, 4) not null default 0,
    current_balance numeric(20, 4) not null default 0,
    current_equity numeric(20, 4) not null default 0,
    timezone text not null default 'UTC',
    session_reference_tz text not null default 'America/New_York',
    risk_mode text not null default 'fixed',
    risk_value numeric(20, 6) not null default 0,
    risk_base text not null default 'initial_balance',
    connection_status text not null default 'pending'
        check (connection_status in ('pending', 'connected', 'stale', 'error')),
    last_sync_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists trading_accounts_user_id_idx on public.trading_accounts(user_id);

create table if not exists public.connector_tokens (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references public.trading_accounts(id) on delete cascade,
    token_hash text not null unique,
    expires_at timestamptz not null,
    revoked_at timestamptz,
    last_used_at timestamptz,
    created_at timestamptz not null default now()
);
create index if not exists connector_tokens_account_id_idx on public.connector_tokens(account_id);

create table if not exists public.raw_deals (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references public.trading_accounts(id) on delete cascade,
    mt5_deal_id text not null,
    mt5_order_id text,
    mt5_position_id text not null,
    entry_type text not null check (entry_type in ('in', 'out', 'inout', 'out_by')),
    symbol text not null,
    direction text not null check (direction in ('buy', 'sell')),
    volume numeric(20, 8) not null default 0,
    price numeric(20, 10) not null default 0,
    profit numeric(20, 4) not null default 0,
    commission numeric(20, 4) not null default 0,
    swap numeric(20, 4) not null default 0,
    deal_time timestamptz not null,
    raw_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint raw_deals_account_mt5_deal_unique unique (account_id, mt5_deal_id)
);
create index if not exists raw_deals_account_position_time_idx
    on public.raw_deals(account_id, mt5_position_id, deal_time);

create table if not exists public.trades (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references public.trading_accounts(id) on delete cascade,
    mt5_position_id text not null,
    symbol text not null,
    direction text not null check (direction in ('buy', 'sell')),
    volume numeric(20, 8),
    entry_price numeric(20, 10),
    exit_price numeric(20, 10),
    entry_at timestamptz,
    exit_at timestamptz,
    holding_seconds bigint,
    is_partial_close boolean not null default false,
    profit numeric(20, 4) not null default 0,
    commission numeric(20, 4) not null default 0,
    swap numeric(20, 4) not null default 0,
    session text,
    day_of_week smallint check (day_of_week between 0 and 6),
    rule_violations text[] not null default '{}',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint trades_account_position_unique unique (account_id, mt5_position_id)
);
create index if not exists trades_account_exit_idx on public.trades(account_id, exit_at desc);
create index if not exists trades_account_symbol_idx on public.trades(account_id, symbol);

alter table public.trading_accounts enable row level security;
alter table public.connector_tokens enable row level security;
alter table public.raw_deals enable row level security;
alter table public.trades enable row level security;

drop policy if exists "Users can view their own trading accounts" on public.trading_accounts;
create policy "Users can view their own trading accounts"
    on public.trading_accounts for select to authenticated
    using (user_id = (select auth.uid()));
drop policy if exists "Users can create their own trading accounts" on public.trading_accounts;
create policy "Users can create their own trading accounts"
    on public.trading_accounts for insert to authenticated
    with check (user_id = (select auth.uid()));
drop policy if exists "Users can update their own trading accounts" on public.trading_accounts;
create policy "Users can update their own trading accounts"
    on public.trading_accounts for update to authenticated
    using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));
drop policy if exists "Users can delete their own trading accounts" on public.trading_accounts;
create policy "Users can delete their own trading accounts"
    on public.trading_accounts for delete to authenticated
    using (user_id = (select auth.uid()));

drop policy if exists "Users can view raw deals for their own accounts" on public.raw_deals;
create policy "Users can view raw deals for their own accounts"
    on public.raw_deals for select to authenticated
    using (exists (
        select 1 from public.trading_accounts a
        where a.id = raw_deals.account_id and a.user_id = (select auth.uid())
    ));

drop policy if exists "Users can view trades for their own accounts" on public.trades;
create policy "Users can view trades for their own accounts"
    on public.trades for select to authenticated
    using (exists (
        select 1 from public.trading_accounts a
        where a.id = trades.account_id and a.user_id = (select auth.uid())
    ));

-- No authenticated/anon policies are created for connector_tokens.
