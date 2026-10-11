-- TradeGrid subscription state and Paystack checkout/webhook idempotency.
-- All financial state is written by authenticated FastAPI endpoints using the
-- service-role client; no browser-side policies are created.

create table if not exists public.user_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references auth.users(id) on delete cascade,
  plan text not null default 'free' check (plan in ('free', 'trader', 'pro', 'enterprise')),
  status text not null default 'inactive' check (status in ('inactive', 'active', 'past_due', 'cancelled', 'trialing')),
  billing_period text check (billing_period is null or billing_period in ('monthly', 'yearly')),
  provider text check (provider is null or provider = 'paystack'),
  provider_customer_code text,
  provider_subscription_code text,
  provider_subscription_email_token text,
  provider_plan_code text,
  checkout_reference text,
  current_period_start timestamptz,
  current_period_end timestamptz,
  cancel_at_period_end boolean not null default false,
  last_payment_at timestamptz,
  last_event_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists user_subscriptions_status_idx on public.user_subscriptions(status, updated_at desc);

create table if not exists public.billing_checkout_intents (
  reference text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  user_email text not null,
  plan text not null check (plan in ('trader', 'pro')),
  billing_period text not null check (billing_period in ('monthly', 'yearly')),
  provider_plan_code text not null,
  expected_amount integer not null check (expected_amount > 0),
  currency text not null check (currency = 'USD'),
  status text not null default 'pending' check (status in ('pending', 'initialized', 'succeeded', 'failed', 'expired')),
  authorization_url text,
  provider_customer_code text,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  safe_error_code text
);
create index if not exists billing_checkout_intents_user_idx on public.billing_checkout_intents(user_id, created_at desc);
create index if not exists billing_checkout_intents_status_idx on public.billing_checkout_intents(status, created_at);

create table if not exists public.billing_webhook_events (
  event_key text primary key,
  event_type text not null,
  processing_status text not null default 'received' check (processing_status in ('received', 'processed', 'failed')),
  safe_error_code text,
  attempts integer not null default 1 check (attempts > 0),
  received_at timestamptz not null default now(),
  processed_at timestamptz
);
create index if not exists billing_webhook_events_status_idx on public.billing_webhook_events(processing_status, received_at);

alter table public.user_subscriptions enable row level security;
alter table public.billing_checkout_intents enable row level security;
alter table public.billing_webhook_events enable row level security;


-- Enforce account caps atomically in PostgreSQL as well as in the API layer,
-- preventing parallel create requests from exceeding the purchased entitlement.
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

drop trigger if exists trading_accounts_plan_limit on public.trading_accounts;
create trigger trading_accounts_plan_limit
before insert on public.trading_accounts
for each row execute function public.enforce_tradegrid_account_limit();
