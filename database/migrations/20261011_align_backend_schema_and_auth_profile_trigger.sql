-- Bring the existing Trading Intelligence database in line with the FastAPI backend.
-- This migration is additive and preserves existing product tables and records.

alter table public.raw_deals
  add column if not exists entry_type text not null default 'in';

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.raw_deals'::regclass
      and conname = 'raw_deals_entry_type_check'
  ) then
    alter table public.raw_deals
      add constraint raw_deals_entry_type_check
      check (entry_type in ('in', 'out', 'inout', 'out_by'));
  end if;
end $$;

-- The current trading_accounts table references profiles(id). Create profiles
-- for future Supabase Auth users, without overwriting existing profiles.
create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, display_name)
  values (
    new.id,
    nullif(trim(coalesce(new.raw_user_meta_data ->> 'display_name', new.raw_user_meta_data ->> 'full_name', '')), '')
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created_profile on auth.users;
create trigger on_auth_user_created_profile
  after insert on auth.users
  for each row execute function public.handle_new_auth_user();

-- Browser users may read only their own data. All writes go through FastAPI.
drop policy if exists trading_accounts_all_own on public.trading_accounts;
drop policy if exists "Users can view their own trading accounts" on public.trading_accounts;
create policy "Users can view their own trading accounts"
  on public.trading_accounts for select to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists connector_tokens_all_own on public.connector_tokens;

drop policy if exists raw_deals_select_own on public.raw_deals;
drop policy if exists "Users can view raw deals for their own accounts" on public.raw_deals;
create policy "Users can view raw deals for their own accounts"
  on public.raw_deals for select to authenticated
  using (exists (
    select 1 from public.trading_accounts a
    where a.id = raw_deals.account_id and a.user_id = (select auth.uid())
  ));

drop policy if exists trades_all_own on public.trades;
drop policy if exists "Users can view trades for their own accounts" on public.trades;
create policy "Users can view trades for their own accounts"
  on public.trades for select to authenticated
  using (exists (
    select 1 from public.trading_accounts a
    where a.id = trades.account_id and a.user_id = (select auth.uid())
  ));
