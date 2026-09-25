-- The kill and the pause switch: stop everything for good, or for now
-- (ADR 023; owner, 2026-09-26).
--
-- The `kill_switch` flag pauses: nothing runs while it is on, and paused
-- work can be resumed afterwards. The owner's words: that is a pause switch.
-- A kill ends everything instead:
--
-- - the pause flag goes on, so nothing new starts and any invocation still
--   working stops at its next model call or step;
-- - every unfinished run and task in the org is cancelled (`killed`), so
--   nothing can be resumed or woken, and nothing resumes when the flag goes off;
-- - every pending approval expires and every held tool call is rejected, so
--   no held action can be approved into running later.
--
-- Triggers and settings are left as they are: turning the flag off starts
-- fresh work on the next schedule, not the killed work.

-- Whether the caller is the backend itself (service_role). Inside a SECURITY
-- DEFINER function current_user is the function's owner, so the checks here
-- read the session's role instead.
create or replace function public.is_backend()
returns boolean
language sql
stable
set search_path = ''
as $$
  select coalesce(current_setting('role', true), '') = 'service_role';
$$;

-- resume_paused_runs (20260926090000) checked current_user, which is never
-- service_role inside a SECURITY DEFINER function: the backend was refused.
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
     or not (public.is_org_member(p_org_id) or public.is_backend()) then
    raise exception 'Only a person in the org may resume its runs' using errcode = 'insufficient_privilege';
  end if;
  if p_reason not in ('kill_switch', 'budget_exceeded', 'agent_disabled', 'department_disabled') then
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

revoke execute on function public.resume_paused_runs(uuid, text) from public, anon;
grant execute on function public.resume_paused_runs(uuid, text) to authenticated, service_role;

create or replace function public.kill_everything(p_org_id uuid, p_note text default null)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  runs_killed integer;
  tasks_killed integer;
  approvals_expired integer;
  result jsonb;
begin
  if public.current_agent_id() is not null
     or not (public.is_org_member(p_org_id) or public.is_backend()) then
    raise exception 'Only a person in the org may kill its work' using errcode = 'insufficient_privilege';
  end if;

  insert into public.system_flags (org_id, key, value)
  values (p_org_id, 'kill_switch', 'true'::jsonb)
  on conflict (org_id, key) do update set value = excluded.value;

  -- Tasks first: a cancelled run would otherwise mark its task failed.
  update public.tasks
     set status = 'cancelled', finished_at = now(), error = 'killed'
   where org_id = p_org_id and status not in ('done', 'failed', 'cancelled');
  get diagnostics tasks_killed = row_count;

  update public.runs
     set status = 'cancelled', stop_reason = 'killed', ended_at = now(),
         lease_expires_at = null, error = coalesce(p_note, 'Killed by the owner')
   where org_id = p_org_id and status not in ('succeeded', 'failed', 'cancelled');
  get diagnostics runs_killed = row_count;

  update public.tool_calls
     set status = 'rejected', error = 'Killed by the owner'
   where org_id = p_org_id and status in ('held', 'approved');

  update public.approvals
     set status = 'expired', verdict = 'killed', decided_at = now(), decided_by = auth.uid()
   where org_id = p_org_id and status = 'pending';
  get diagnostics approvals_expired = row_count;

  result := jsonb_build_object('runs', runs_killed, 'tasks', tasks_killed,
                               'approvals', approvals_expired, 'note', p_note,
                               'killed_by', auth.uid());
  insert into public.events (org_id, type, payload) values (p_org_id, 'killed', result);
  return result;
end;
$$;

revoke execute on function public.kill_everything(uuid, text) from public, anon;
grant execute on function public.kill_everything(uuid, text) to authenticated, service_role;

-- The pause switch, for the owner (the flag was service-role only). Pausing
-- keeps work where it is; resume_paused_runs() carries it on afterwards.
create or replace function public.set_pause(p_org_id uuid, p_on boolean)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  if public.current_agent_id() is not null
     or not (public.is_org_member(p_org_id) or public.is_backend()) then
    raise exception 'Only a person in the org may pause it' using errcode = 'insufficient_privilege';
  end if;
  insert into public.system_flags (org_id, key, value)
  values (p_org_id, 'kill_switch', to_jsonb(p_on))
  on conflict (org_id, key) do update set value = excluded.value;
  insert into public.events (org_id, type, payload)
  values (p_org_id, case when p_on then 'paused' else 'unpaused' end,
          jsonb_build_object('by', auth.uid()));
  return p_on;
end;
$$;

revoke execute on function public.set_pause(uuid, boolean) from public, anon;
grant execute on function public.set_pause(uuid, boolean) to authenticated, service_role;
