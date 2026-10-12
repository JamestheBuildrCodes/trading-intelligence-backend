-- This function is trigger-only. Do not expose it as a public PostgREST RPC.
revoke execute on function public.enforce_tradegrid_account_limit() from public, anon, authenticated;
