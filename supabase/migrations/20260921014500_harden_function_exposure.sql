-- Hardening, from the advisories Supabase raised once the schema was applied.
--
-- A follow-up migration rather than an edit to the three before it: those have
-- now run against the real database, so they are history and stay as written.

-- set_updated_at was the one function whose search_path was left unpinned.
-- It is SECURITY INVOKER, so the exposure is smaller than for the two
-- definer functions, but a mutable search_path on a trigger that fires on
-- every write is not worth leaving open. The empty path forces every name to
-- resolve explicitly; now() lives in pg_catalog, which is always searched.
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- PostgreSQL grants EXECUTE on new functions to PUBLIC by default, which in a
-- Supabase project means anon can reach them over PostgREST as RPC endpoints.
-- is_org_member leaks nothing to an anonymous caller (auth.uid() is null, so
-- it returns false), but kill_switch_on would answer questions about an org
-- id to anyone who guessed one. Neither is meant to be a public endpoint.
revoke execute on function public.is_org_member(uuid) from public;
revoke execute on function public.kill_switch_on(uuid) from public;

grant execute on function public.is_org_member(uuid) to authenticated, service_role;
grant execute on function public.kill_switch_on(uuid) to authenticated, service_role;
