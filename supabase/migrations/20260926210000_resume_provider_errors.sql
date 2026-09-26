-- Two gaps found on 2026-09-26, when OpenRouter ran out of credit mid-run.
--
-- 1. A run paused by a provider error that outlived its five automatic
--    retries could not be resumed by the owner: resume_paused_runs now also
--    takes 'upstream_error' (after topping up credit, say).
-- 2. Running out of credit is a problem the owner must see at once. The
--    brief now reports it first, and writes itself without a model when the
--    model cannot be reached (code, app/agents/digest.py).

create or replace function public.resume_paused_runs(p_org_id uuid, p_reason text default 'kill_switch')
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  resumed integer;
begin
  if public.current_agent_id() is not null
     or not (public.is_org_member(p_org_id) or current_user = 'service_role') then
    raise exception 'Only a person in the org may resume its runs' using errcode = 'insufficient_privilege';
  end if;
  if p_reason not in ('kill_switch', 'budget_exceeded', 'agent_disabled', 'department_disabled',
                      'upstream_error') then
    raise exception 'Runs paused for % are not resumed this way', p_reason using errcode = 'check_violation';
  end if;
  if public.kill_switch_on(p_org_id) then
    raise exception 'The kill switch is still on' using errcode = 'check_violation';
  end if;
  update public.runs
     set stop_reason = 'resumed', wake_count = 0, last_wake_at = null
   where org_id = p_org_id and status = 'paused' and stop_reason = p_reason;
  get diagnostics resumed = row_count;
  insert into public.events (org_id, type, payload)
  values (p_org_id, 'runs_resumed',
          jsonb_build_object('reason', p_reason, 'count', resumed, 'resumed_by', auth.uid()));
  return resumed;
end;
$$;
