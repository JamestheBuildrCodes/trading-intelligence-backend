-- The auth trigger can invoke this function, but API roles must not call it
-- directly through PostgREST. Add indexes used by the backend's account and
-- trade-history queries and cover foreign-key lookups.

revoke execute on function public.handle_new_auth_user() from public, anon, authenticated;

create index if not exists trading_accounts_user_id_idx
  on public.trading_accounts(user_id);
create index if not exists connector_tokens_account_id_idx
  on public.connector_tokens(account_id);
create index if not exists raw_deals_account_position_time_idx
  on public.raw_deals(account_id, mt5_position_id, deal_time);
create index if not exists trades_account_exit_idx
  on public.trades(account_id, exit_at desc);
create index if not exists trades_account_symbol_idx
  on public.trades(account_id, symbol);
