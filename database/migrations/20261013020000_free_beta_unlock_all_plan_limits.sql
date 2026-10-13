-- TEMPORARY FREE-BETA OVERRIDE (2026-10-13).
-- Application entitlements unlock all features during beta. This trigger override also
-- removes the database-level account-count cap so API and database policy stay aligned.
-- Restore the normal plan-aware trigger before ending the beta.
create or replace function public.enforce_tradegrid_account_limit()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  -- Preserve per-user serialization and the tenant's auth row lock.
  perform 1
  from auth.users
  where id = new.user_id
  for update;

  -- All subscription tiers are temporarily treated as unrestricted during beta.
  -- Ownership, foreign keys, RLS and authentication remain enforced elsewhere.
  return new;
end;
$$;
