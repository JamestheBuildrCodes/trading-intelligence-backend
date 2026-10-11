-- Hosted MT5 connection state for the MetaApi cloud adapter.
-- No broker passwords are stored by TradeGrid in these columns.

alter table public.trading_accounts
  add column if not exists connection_provider text,
  add column if not exists provider_account_id text,
  add column if not exists hosted_connection_status text not null default 'not_configured',
  add column if not exists hosted_last_error_code text,
  add column if not exists hosted_history_cursor_at timestamptz,
  add column if not exists hosted_first_deal_at timestamptz,
  add column if not exists hosted_last_deal_at timestamptz,
  add column if not exists hosted_connection_verified_at timestamptz,
  add column if not exists hosted_last_sync_at timestamptz;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'trading_accounts_connection_provider_check'
      and conrelid = 'public.trading_accounts'::regclass
  ) then
    alter table public.trading_accounts
      add constraint trading_accounts_connection_provider_check
      check (connection_provider is null or connection_provider in ('local_bridge', 'metaapi'));
  end if;
  if not exists (
    select 1 from pg_constraint
    where conname = 'trading_accounts_hosted_status_check'
      and conrelid = 'public.trading_accounts'::regclass
  ) then
    alter table public.trading_accounts
      add constraint trading_accounts_hosted_status_check
      check (hosted_connection_status in ('not_configured', 'provisioning', 'connecting', 'connected', 'syncing', 'error', 'disconnected'));
  end if;
end $$;

create index if not exists trading_accounts_hosted_sync_idx
  on public.trading_accounts(hosted_connection_status, hosted_last_sync_at)
  where connection_provider = 'metaapi' and provider_account_id is not null;
