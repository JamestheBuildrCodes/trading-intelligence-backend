-- Prevent browser-side authenticated clients from creating, editing, or
-- deleting account records directly. Account mutations go through FastAPI,
-- which validates the user and owns the sync-controlled balance/status fields.
alter table public.trading_accounts enable row level security;

drop policy if exists "Users can create their own trading accounts" on public.trading_accounts;
drop policy if exists "Users can update their own trading accounts" on public.trading_accounts;
drop policy if exists "Users can delete their own trading accounts" on public.trading_accounts;
