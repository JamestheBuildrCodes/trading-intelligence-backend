-- Serialize account creation per user so concurrent requests cannot race past plan caps.
-- Acquiring the auth.users row lock makes subsequent count checks run one-at-a-time
-- for a given user while allowing different users to create accounts concurrently.

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
  v_max_accounts integer := 1;
  v_count integer := 0;
begin
  -- This row lock serializes all account inserts for the same tenant.
  perform 1
  from auth.users
  where id = new.user_id
  for update;

  select plan, status, current_period_end
    into v_plan, v_status, v_period_end
  from public.user_subscriptions
  where user_id = new.user_id
  limit 1;

  if not found then
    v_plan := 'free';
    v_status := 'inactive';
    v_period_end := null;
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

  if v_max_accounts is null then
    return new;
  end if;

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
