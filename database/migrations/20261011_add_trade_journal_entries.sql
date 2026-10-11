-- Persistent, account-scoped journal annotations. MT5-synced trades remain source-of-truth
-- for execution facts; journal fields are stored separately so re-syncs never overwrite notes.
create table if not exists public.trade_journal_entries (
    id uuid primary key default gen_random_uuid(),
    account_id uuid not null references public.trading_accounts(id) on delete cascade,
    trade_id uuid not null references public.trades(id) on delete cascade,
    setup_name text not null default '' check (char_length(setup_name) <= 120),
    thesis text not null default '' check (char_length(thesis) <= 4000),
    pre_trade_emotion text not null default '' check (char_length(pre_trade_emotion) <= 80),
    post_trade_emotion text not null default '' check (char_length(post_trade_emotion) <= 80),
    followed_plan boolean,
    planned_risk_amount numeric(20, 4) check (planned_risk_amount is null or planned_risk_amount >= 0),
    planned_reward_r numeric(12, 4) check (planned_reward_r is null or planned_reward_r >= 0),
    mistake_tags text[] not null default '{}',
    review_notes text not null default '' check (char_length(review_notes) <= 4000),
    updated_at timestamptz not null default now(),
    constraint trade_journal_account_trade_unique unique (account_id, trade_id)
);
create index if not exists trade_journal_account_updated_idx
    on public.trade_journal_entries(account_id, updated_at desc);
alter table public.trade_journal_entries enable row level security;
-- No browser policies: access only through the authenticated API after ownership checks.
