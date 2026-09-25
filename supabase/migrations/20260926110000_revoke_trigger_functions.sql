-- Trigger functions are not an API. The Supabase security advisor flagged
-- these SECURITY DEFINER trigger functions as callable over /rest/v1/rpc by
-- anon and signed-in users. Postgres refuses to run a trigger function
-- outside a trigger, so nothing could be done with them; this removes the
-- exposure anyway. Triggers still fire: a trigger does not need EXECUTE.

revoke execute on function public.tasks_guard() from public, anon, authenticated;
revoke execute on function public.task_follows_run() from public, anon, authenticated;
revoke execute on function public.task_status_changed() from public, anon, authenticated;
revoke execute on function public.facts_require_admission() from public, anon, authenticated;
