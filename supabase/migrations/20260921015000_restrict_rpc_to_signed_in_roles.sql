-- Revoking EXECUTE from PUBLIC was not enough to keep anon out.
--
-- Supabase configures default privileges on the public schema that grant
-- EXECUTE on new functions to anon, authenticated and service_role by name.
-- A revoke from PUBLIC removes only the implicit grant every function starts
-- with; the explicit per-role grant survives it, so anon could still reach
-- these over PostgREST as /rest/v1/rpc/... Revoking from anon by name is what
-- actually closes it.
revoke execute on function public.is_org_member(uuid) from anon;
revoke execute on function public.kill_switch_on(uuid) from anon;

-- authenticated keeps EXECUTE deliberately, and Supabase's linter will go on
-- reporting that as a finding. RLS policy expressions are evaluated with the
-- querying role's privileges, and every policy in this schema calls
-- is_org_member, so revoking it from authenticated would not harden anything
-- -- it would break every read in the database.
