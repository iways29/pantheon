-- How hard each tier's model "thinks" before answering (owner, 2026-09-26).
--
-- Reasoning models spend output tokens thinking, and on OpenRouter those
-- count against a call's max_tokens. On 2026-09-26 the cheap tier (default
-- effort "high") spent every turn's budget thinking, so its tool calls were
-- cut off and a research run looped. The effort per tier is data: the
-- `reasoning` flag, {"<tier>": {"effort": "low"}}, sent by the gateway as
-- OpenRouter's `reasoning` parameter. A tier without an entry sends nothing
-- and gets the model's default. Changes are events.

insert into public.system_flags (org_id, key, value)
select id, 'reasoning', '{"cheap": {"effort": "low"}, "standard": {"effort": "low"}}'::jsonb
  from public.orgs
on conflict (org_id, key) do nothing;

create or replace function public.audit_reasoning_flag()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.key <> 'reasoning'
     or (tg_op = 'UPDATE' and new.value is not distinct from old.value) then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'reasoning_changed',
          jsonb_build_object('value', new.value, 'changed_by', auth.uid(),
                             'db_role', current_user));
  return new;
end;
$$;

revoke execute on function public.audit_reasoning_flag() from public, anon, authenticated;

create trigger system_flags_audit_reasoning
  after insert or update on public.system_flags
  for each row execute function public.audit_reasoning_flag();

-- A run paused by a passing provider error (a 429 rate limit, a 5xx) is retried
-- like one paused at its deadline: at most five wakes, 45 seconds apart
-- (2026-09-26: a rate-limited model otherwise stopped a morning for good).
create or replace function public.pending_wakeups(p_limit integer default 20)
returns setof uuid
language sql
set search_path = ''
as $$
  update public.runs r
     set wake_count = r.wake_count + 1,
         last_wake_at = now()
   where r.id in (
     select x.id
       from public.runs x
      where x.trigger in ('schedule', 'task')
        and x.wake_count < 5
        and (x.last_wake_at is null or x.last_wake_at < now() - interval '45 seconds')
        and not public.kill_switch_on(x.org_id)
        and (
          x.status = 'pending'
          or (x.status = 'paused'
              and x.stop_reason in ('deadline', 'approved', 'redirected', 'resumed',
                                  'upstream_error'))
          or (x.status = 'paused' and x.stop_reason = 'awaiting_approval'
              and not exists (select 1 from public.approvals ap
                               where ap.run_id = x.id and ap.status = 'pending'))
          or (x.status = 'running' and x.lease_expires_at < now())
        )
      order by x.created_at
      limit p_limit
        for update skip locked
   )
  returning r.id;
$$;
