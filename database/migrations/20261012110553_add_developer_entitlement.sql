-- Developer-only Tradvoryx entitlement for the verified owner account.
-- This does not create an admin-code endpoint and cannot be granted by browser input.
alter table public.user_subscriptions
  add column if not exists developer_access boolean not null default false,
  add column if not exists developer_preview_plan text not null default 'enterprise'
    check (developer_preview_plan in ('free', 'trader', 'pro', 'enterprise'));

-- Grant developer access only to the pre-authorized owner email.
-- If the owner has not signed up yet, rerun this migration after signup.
insert into public.user_subscriptions (user_id, plan, status, developer_access, developer_preview_plan)
select u.id, 'free', 'inactive', true, 'enterprise'
from auth.users u
where lower(u.email) = lower('preciousjames517@gmail.com')
on conflict (user_id) do update
set developer_access = true,
    developer_preview_plan = coalesce(public.user_subscriptions.developer_preview_plan, 'enterprise'),
    updated_at = now();

-- Preserve serialized account-limit enforcement but allow the authorized developer
-- to create test accounts regardless of the currently previewed tier.
create or replace function public.enforce_tradegrid_account_limit()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_plan text := 'free';
  v_status text := 'inactive';
  v_period_end timestamptz;
  v_developer_access boolean := false;
  v_max_accounts integer := 1;
  v_count integer := 0;
begin
  perform 1 from auth.users where id = new.user_id for update;

  select plan, status, current_period_end, developer_access
    into v_plan, v_status, v_period_end, v_developer_access
  from public.user_subscriptions
  where user_id = new.user_id
  limit 1;

  if not found then
    v_plan := 'free';
    v_status := 'inactive';
    v_period_end := null;
    v_developer_access := false;
  end if;

  if v_developer_access then
    return new;
  end if;

  if v_status not in ('active', 'trialing')
     or v_period_end is null
     or v_period_end <= now() then
    v_plan := 'free';
  end if;

  v_max_accounts := case v_plan
    when 'trader' then 3
    when 'pro' then 10
    when 'enterprise' then null
    else 1
  end;

  if v_max_accounts is null then return new; end if;

  select count(*) into v_count
  from public.trading_accounts
  where user_id = new.user_id;

  if v_count >= v_max_accounts then
    raise exception 'plan_limit_reached'
      using errcode = 'P0001',
            detail = format('Plan %s allows at most %s trading accounts.', v_plan, v_max_accounts);
  end if;

  return new;
end;
$$;
