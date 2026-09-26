-- Step 8.1b: tomorrow's question (owner, 2026-09-26).
--
-- Standing topics run every morning on their own. On top, the owner can ask
-- for something specific: a `routine_requests` row for a routine (for example
-- research:morning-brief). The next time that routine fires, every pending
-- request joins its task (input.owner_requests and the instructions) once and
-- is marked used. Only a person adds or cancels a request; each is an event.

create table public.routine_requests (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  -- triggers.routine_key, e.g. research:morning-brief.
  routine_key text not null check (routine_key ~ '^[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*$'),
  request text not null check (length(btrim(request)) between 3 and 1000),
  status text not null default 'pending' check (status in ('pending', 'used', 'cancelled')),
  task_id uuid references public.tasks (id) on delete set null,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default clock_timestamp(),
  used_at timestamptz
);

comment on table public.routine_requests is
  'The owner''s one-off asks for the next run of a morning routine; used once.';

create index routine_requests_pending
  on public.routine_requests (org_id, routine_key) where status = 'pending';

create or replace function public.audit_routine_request()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE' and new.status is not distinct from old.status then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'routine_request_' || new.status,
          jsonb_build_object('request_id', new.id, 'routine', new.routine_key,
                             'request', new.request, 'task_id', new.task_id,
                             'by', auth.uid()));
  return new;
end;
$$;

revoke execute on function public.audit_routine_request() from public, anon, authenticated;

create trigger routine_requests_audit
  after insert or update on public.routine_requests
  for each row execute function public.audit_routine_request();

alter table public.routine_requests enable row level security;
create policy routine_requests_select on public.routine_requests
  for select to authenticated using (public.is_org_member(org_id));
create policy routine_requests_write on public.routine_requests
  for all to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null)
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
grant select, insert, update on public.routine_requests to authenticated;
grant select, insert, update, delete on public.routine_requests to service_role;

-- The routine's task carries the owner's pending requests, once.
create or replace function public.dispatch_due_triggers(p_now timestamptz default now())
returns setof uuid
language plpgsql
set search_path = ''
as $$
declare
  t record;
  local_now timestamp;
  local_date date;
  slot_start timestamp;
  new_task uuid;
  asks jsonb;
  task_input jsonb;
  task_instructions text;
begin
  for t in
    select tr.*
      from public.triggers tr
      join public.agents a on a.id = tr.agent_id and a.org_id = tr.org_id
      join public.departments d on d.id = a.department_id and d.org_id = a.org_id
     where tr.enabled and a.enabled and d.enabled
     order by tr.created_at
       for update of tr skip locked
  loop
    if public.kill_switch_on(t.org_id) then
      continue;
    end if;
    local_now := p_now at time zone t.timezone;
    local_date := local_now::date;
    slot_start := local_date + t.time_of_day;
    if extract(dow from local_now)::smallint <> all (t.days_of_week)
       or local_now < slot_start
       or local_now >= slot_start + make_interval(mins => t.grace_minutes)
       or (t.last_slot is not null and t.last_slot >= local_date)
    then
      continue;
    end if;

    select coalesce(jsonb_agg(r.request order by r.created_at), '[]'::jsonb) into asks
      from public.routine_requests r
     where r.org_id = t.org_id and r.routine_key = t.routine_key and r.status = 'pending'
       and r.created_at <= p_now;
    task_input := t.task;
    task_instructions := coalesce(t.task->>'question', t.task->>'instructions', '');
    if jsonb_array_length(asks) > 0 then
      task_input := task_input || jsonb_build_object('owner_requests', asks);
      task_instructions := task_instructions || E'\nThe owner also asked for today: '
        || (select string_agg(value, '; ') from jsonb_array_elements_text(asks));
    end if;

    insert into public.tasks
      (org_id, assigned_agent_id, department_id, created_by, title, instructions, input,
       max_steps, max_tokens, idempotency_key, requested_by)
    values
      (t.org_id, t.agent_id, (select department_id from public.agents where id = t.agent_id),
       'trigger:' || t.id, t.name, task_instructions,
       task_input, t.max_steps, t.max_tokens, 'trigger:' || t.id || ':' || local_date, t.run_as)
    on conflict (org_id, idempotency_key) do nothing
    returning id into new_task;

    update public.triggers set last_slot = local_date where id = t.id;

    if new_task is not null then
      update public.routine_requests
         set status = 'used', task_id = new_task, used_at = p_now
       where org_id = t.org_id and routine_key = t.routine_key and status = 'pending'
         and created_at <= p_now;
      insert into public.events (org_id, agent_id, type, payload)
      values (t.org_id, t.agent_id, 'trigger_fired',
              jsonb_build_object('trigger_id', t.id, 'name', t.name, 'slot', local_date,
                                 'task_id', new_task,
                                 'owner_requests', jsonb_array_length(asks)));
      return next new_task;
    end if;
  end loop;
end;
$$;
