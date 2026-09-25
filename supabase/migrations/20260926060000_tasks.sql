-- Step 7.3: tasks and delegation, in the database (ADR 019).
--
-- Owner decision 5 (2026-09-26): the chain of command (Chief of Staff, heads,
-- workers) is built from durable task rows, not in-process calls. A task is a
-- piece of work someone asked for; a run is one attempt to advance it. A head
-- creates tasks for its workers and stops; when the last one finishes, its own
-- task is queued again and its next run sees their results. Nothing waits in
-- memory, so every step fits a short serverless invocation.
--
-- Owner decision 13 (2026-09-26): depth 3, at most 4 children per task, at
-- most 50 tasks per department per day, held as data and enforced here.

-- --- Who may delegate ------------------------------------------------------

alter table public.agents
  add column role_type text not null default 'worker'
    check (role_type in ('chief_of_staff', 'head', 'worker'));

comment on column public.agents.role_type is
  'chief_of_staff and head may create tasks; a worker may not.';

-- --- Delegation limits, per org -----------------------------------------------

create table public.delegation_limits (
  org_id uuid primary key references public.orgs (id) on delete cascade,
  -- Levels below the root: 3 means root (0), head (1), worker (2).
  max_depth integer not null default 3 check (max_depth between 1 and 6),
  max_children integer not null default 4 check (max_children between 1 and 20),
  max_tasks_per_department_per_day integer not null default 50
    check (max_tasks_per_department_per_day between 1 and 1000),
  updated_at timestamptz not null default now()
);

comment on table public.delegation_limits is
  'Owner-set limits on task trees. No row means the defaults (3, 4, 50).';

create trigger delegation_limits_set_updated_at
  before update on public.delegation_limits
  for each row execute function public.set_updated_at();

create or replace function public.audit_delegation_limits()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'delegation_limits_changed', jsonb_build_object(
    'max_depth', new.max_depth,
    'max_children', new.max_children,
    'max_tasks_per_department_per_day', new.max_tasks_per_department_per_day,
    'changed_by', auth.uid(), 'db_role', current_user));
  return new;
end;
$$;

create trigger delegation_limits_audit
  after insert or update on public.delegation_limits
  for each row execute function public.audit_delegation_limits();

alter table public.delegation_limits enable row level security;
create policy delegation_limits_select on public.delegation_limits
  for select to authenticated using (public.is_org_member(org_id));
create policy delegation_limits_write on public.delegation_limits
  for all to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null)
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
grant select, insert, update on public.delegation_limits to authenticated;
grant select, insert, update, delete on public.delegation_limits to service_role;

insert into public.delegation_limits (org_id) select id from public.orgs
on conflict (org_id) do nothing;

-- --- Tasks -------------------------------------------------------------------

create table public.tasks (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  parent_task_id uuid,
  -- Set by the guard below; never trusted from the caller.
  root_task_id uuid,
  depth integer not null default 0 check (depth >= 0),
  department_id uuid not null,
  assigned_agent_id uuid not null,
  -- owner, trigger:<id>, or agent:<id>
  created_by text not null,
  created_by_agent_id uuid,
  title text not null check (btrim(title) <> '' and length(title) <= 200),
  instructions text not null default '',
  input jsonb not null default '{}'::jsonb,
  status text not null default 'queued' check (status in
    ('queued', 'running', 'blocked', 'awaiting_approval', 'done', 'failed', 'cancelled')),
  result jsonb,
  error text,
  -- This task's own ceiling. A child's must fit in what its parent has left.
  max_cost_usd numeric(10, 4) check (max_cost_usd >= 0),
  -- Per-run caps for the runs that advance it.
  max_steps integer not null default 25 check (max_steps > 0),
  max_tokens integer not null default 50000 check (max_tokens > 0),
  attempts integer not null default 0 check (attempts >= 0),
  priority integer not null default 0,
  due_at timestamptz,
  idempotency_key text not null,
  requested_by uuid not null references auth.users (id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz,
  unique (org_id, idempotency_key),
  unique (id, org_id),
  foreign key (parent_task_id, org_id) references public.tasks (id, org_id) on delete cascade,
  foreign key (department_id, org_id) references public.departments (id, org_id) on delete cascade,
  foreign key (assigned_agent_id, org_id) references public.agents (id, org_id) on delete cascade,
  foreign key (created_by_agent_id, org_id) references public.agents (id, org_id) on delete set null (created_by_agent_id)
);

comment on table public.tasks is
  'Durable work orders: a tree of tasks (owner or trigger, then heads, then '
  'workers). Runs advance them. Limits are enforced by tasks_guard.';

create index tasks_org_status_idx on public.tasks (org_id, status, priority desc, created_at);
create index tasks_parent_idx on public.tasks (parent_task_id);
create index tasks_department_day_idx on public.tasks (department_id, created_at);

create trigger tasks_set_updated_at
  before update on public.tasks
  for each row execute function public.set_updated_at();

-- Every new task passes here. SECURITY DEFINER so its counts see the whole
-- org, not only what the caller's RLS shows; it reads and raises, nothing else.
create or replace function public.tasks_guard()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  limits public.delegation_limits;
  parent public.tasks;
  creator_role text;
  agent_dept uuid;
  siblings integer;
  today integer;
  committed numeric;
begin
  select * into limits from public.delegation_limits where org_id = new.org_id;
  if not found then
    limits.max_depth := 3;
    limits.max_children := 4;
    limits.max_tasks_per_department_per_day := 50;
  end if;

  select department_id into agent_dept
    from public.agents where id = new.assigned_agent_id and org_id = new.org_id;
  if agent_dept is null then
    raise exception 'No agent % in this org', new.assigned_agent_id using errcode = 'foreign_key_violation';
  end if;
  new.department_id := agent_dept;

  if new.created_by_agent_id is not null then
    select role_type into creator_role
      from public.agents where id = new.created_by_agent_id and org_id = new.org_id;
    if creator_role is null or creator_role not in ('chief_of_staff', 'head') then
      raise exception 'Only the Chief of Staff and heads may create tasks'
        using errcode = 'insufficient_privilege';
    end if;
    if new.parent_task_id is null then
      raise exception 'An agent may only create tasks under one it is working on'
        using errcode = 'check_violation';
    end if;
  end if;

  if new.parent_task_id is null then
    new.depth := 0;
    new.root_task_id := new.id;
  else
    select * into parent from public.tasks
     where id = new.parent_task_id and org_id = new.org_id for update;
    if not found then
      raise exception 'No parent task %', new.parent_task_id using errcode = 'foreign_key_violation';
    end if;
    if new.created_by_agent_id is not null and parent.assigned_agent_id <> new.created_by_agent_id then
      raise exception 'An agent may only split its own task' using errcode = 'insufficient_privilege';
    end if;
    new.depth := parent.depth + 1;
    new.root_task_id := coalesce(parent.root_task_id, parent.id);
    if new.depth >= limits.max_depth then
      raise exception 'Delegation too deep: at most % levels', limits.max_depth
        using errcode = 'check_violation';
    end if;
    select count(*) into siblings from public.tasks
     where parent_task_id = parent.id and status <> 'cancelled';
    if siblings >= limits.max_children then
      raise exception 'Task % already has % sub-tasks (the limit)', parent.id, siblings
        using errcode = 'check_violation';
    end if;
    if parent.max_cost_usd is not null then
      if new.max_cost_usd is null then
        raise exception 'A sub-task of a budgeted task needs its own budget'
          using errcode = 'check_violation';
      end if;
      select coalesce(sum(max_cost_usd), 0) into committed from public.tasks
       where parent_task_id = parent.id and status <> 'cancelled';
      if new.max_cost_usd > parent.max_cost_usd - public.task_spent_usd(parent.id) - committed then
        raise exception 'Sub-task budget $% exceeds what task % has left', new.max_cost_usd, parent.id
          using errcode = 'check_violation';
      end if;
    end if;
  end if;

  select count(*) into today from public.tasks
   where department_id = new.department_id
     and created_at >= date_trunc('day', now() at time zone 'utc');
  if today >= limits.max_tasks_per_department_per_day then
    raise exception 'Department has reached % tasks today', limits.max_tasks_per_department_per_day
      using errcode = 'check_violation';
  end if;

  return new;
end;
$$;

-- --- Runs belong to tasks --------------------------------------------------

alter table public.runs
  add column task_id uuid,
  add constraint runs_task_id_org_id_fkey
    foreign key (task_id, org_id) references public.tasks (id, org_id) on delete set null (task_id);

create index runs_task_idx on public.runs (task_id);

-- What a task's own runs have cost.
create or replace function public.task_spent_usd(p_task_id uuid)
returns numeric
language sql
stable
set search_path = ''
as $$
  select coalesce(sum(mc.cost_usd), 0)
    from public.model_calls mc join public.runs r on r.id = mc.run_id
   where r.task_id = p_task_id;
$$;

create trigger tasks_guard
  before insert on public.tasks
  for each row execute function public.tasks_guard();

-- Cost of a task and of its whole subtree, for the owner and the Milestone.
create or replace view public.task_costs
with (security_invoker = true)
as
with recursive tree as (
  select t.id as task_id, t.id as member_id from public.tasks t
  union all
  select tree.task_id, c.id from tree join public.tasks c on c.parent_task_id = tree.member_id
)
select t.id as task_id, t.org_id, t.title, t.depth, t.status,
       public.task_spent_usd(t.id) as own_cost_usd,
       (select coalesce(sum(public.task_spent_usd(m.member_id)), 0)
          from tree m where m.task_id = t.id) as tree_cost_usd
  from public.tasks t;

-- --- Lifecycle ---------------------------------------------------------------

-- When a run that advances a task stops, the task follows: done if it has no
-- unfinished children, blocked if it does, failed if the run failed. A paused
-- run leaves the task running; it resumes later.
create or replace function public.task_follows_run()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  open_children integer;
begin
  if new.task_id is null or new.status = old.status then
    return new;
  end if;
  if new.status = 'succeeded' then
    select count(*) into open_children from public.tasks
     where parent_task_id = new.task_id
       and status not in ('done', 'failed', 'cancelled');
    update public.tasks
       set status = case when open_children > 0 then 'blocked' else 'done' end,
           result = coalesce(result, new.output),
           finished_at = case when open_children > 0 then null else now() end
     where id = new.task_id and status in ('running', 'queued');
  elsif new.status in ('failed', 'cancelled') then
    update public.tasks
       set status = 'failed', error = coalesce(new.error, new.stop_reason), finished_at = now()
     where id = new.task_id and status in ('running', 'queued');
  end if;
  return new;
end;
$$;

create trigger runs_task_follows
  after update of status on public.runs
  for each row execute function public.task_follows_run();

-- When a task finishes, its blocked parent wakes once its last child has.
-- Every change of task status is an event.
create or replace function public.task_status_changed()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE' and new.status = old.status then
    return new;
  end if;
  insert into public.events (org_id, agent_id, type, payload)
  values (new.org_id, new.assigned_agent_id, 'task_' || new.status,
          jsonb_build_object('task_id', new.id, 'title', new.title, 'depth', new.depth,
                             'parent_task_id', new.parent_task_id, 'created_by', new.created_by));
  if new.status in ('done', 'failed', 'cancelled') and new.parent_task_id is not null then
    update public.tasks p
       set status = 'queued'
     where p.id = new.parent_task_id
       and p.status = 'blocked'
       and not exists (
         select 1 from public.tasks c
          where c.parent_task_id = p.id and c.status not in ('done', 'failed', 'cancelled'));
  end if;
  return new;
end;
$$;

create trigger tasks_status_changed
  after insert or update of status on public.tasks
  for each row execute function public.task_status_changed();

-- --- RLS --------------------------------------------------------------------

alter table public.tasks enable row level security;
create policy tasks_select on public.tasks
  for select to authenticated using (public.is_org_member(org_id));
create policy tasks_insert on public.tasks
  for insert to authenticated with check (public.is_org_member(org_id));
create policy tasks_update on public.tasks
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));
grant select, insert, update on public.tasks to authenticated;
grant select, insert, update, delete on public.tasks to service_role;
grant select on public.task_costs to authenticated, service_role;
revoke execute on function public.task_spent_usd(uuid) from anon;

-- --- Agents cannot make agents or schedules ---------------------------------

-- An agent's session (pantheon.agent_id set) may not create or change agents
-- or triggers. Only a person may.
drop policy agents_insert on public.agents;
drop policy agents_update on public.agents;
create policy agents_insert on public.agents
  for insert to authenticated
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
create policy agents_update on public.agents
  for update to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null)
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);

drop policy triggers_insert on public.triggers;
drop policy triggers_update on public.triggers;
drop policy triggers_delete on public.triggers;
create policy triggers_insert on public.triggers
  for insert to authenticated
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
create policy triggers_update on public.triggers
  for update to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null)
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
create policy triggers_delete on public.triggers
  for delete to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null);

-- --- The scheduler: triggers make tasks, queued tasks get runs --------------

-- A trigger now creates a task from its template (its task JSON), keyed by the
-- trigger and the local date so a slot is one task however often it is asked.
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

    insert into public.tasks
      (org_id, assigned_agent_id, department_id, created_by, title, instructions, input,
       max_steps, max_tokens, idempotency_key, requested_by)
    values
      (t.org_id, t.agent_id, (select department_id from public.agents where id = t.agent_id),
       'trigger:' || t.id, t.name, coalesce(t.task->>'question', t.task->>'instructions', ''),
       t.task, t.max_steps, t.max_tokens, 'trigger:' || t.id || ':' || local_date, t.run_as)
    on conflict (org_id, idempotency_key) do nothing
    returning id into new_task;

    update public.triggers set last_slot = local_date where id = t.id;

    if new_task is not null then
      insert into public.events (org_id, agent_id, type, payload)
      values (t.org_id, t.agent_id, 'trigger_fired',
              jsonb_build_object('trigger_id', t.id, 'name', t.name, 'slot', local_date,
                                 'task_id', new_task));
      return next new_task;
    end if;
  end loop;
end;
$$;

-- Give every queued task a run, if its agent, department and org may work.
-- Each attempt has its own run, keyed by task and attempt number.
create or replace function public.dispatch_queued_tasks(p_limit integer default 20)
returns setof uuid
language plpgsql
set search_path = ''
as $$
declare
  t record;
  new_run uuid;
begin
  for t in
    select tk.*
      from public.tasks tk
      join public.agents a on a.id = tk.assigned_agent_id
      join public.departments d on d.id = a.department_id
     where tk.status = 'queued' and a.enabled and d.enabled
     order by tk.priority desc, tk.created_at
     limit p_limit
       for update of tk skip locked
  loop
    if public.kill_switch_on(t.org_id) then
      continue;
    end if;
    insert into public.runs
      (org_id, agent_id, trigger, idempotency_key, input, requested_by, max_steps,
       max_tokens, task_id)
    values
      (t.org_id, t.assigned_agent_id, 'task', 'task:' || t.id || ':' || (t.attempts + 1),
       t.input || jsonb_build_object('task_id', t.id, 'title', t.title,
                                     'instructions', t.instructions),
       t.requested_by, t.max_steps, t.max_tokens, t.id)
    on conflict (org_id, idempotency_key) do nothing
    returning id into new_run;
    update public.tasks set status = 'running', attempts = attempts + 1 where id = t.id;
    if new_run is not null then
      return next new_run;
    end if;
  end loop;
end;
$$;

-- Wake runs started by triggers or by tasks.
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
        and (
          x.status = 'pending'
          or (x.status = 'paused' and x.stop_reason = 'deadline')
          or (x.status = 'running' and x.lease_expires_at < now())
        )
      order by x.created_at
      limit p_limit
        for update skip locked
   )
  returning r.id;
$$;

create or replace function public.pantheon_tick()
returns void
language plpgsql
set search_path = ''
as $$
begin
  perform public.dispatch_due_triggers();
  perform public.dispatch_queued_tasks();
  perform public.push_runs();
end;
$$;

revoke execute on function public.dispatch_queued_tasks(integer) from public, anon, authenticated;
grant execute on function public.dispatch_queued_tasks(integer) to service_role;
revoke execute on function public.pantheon_tick() from public, anon, authenticated, service_role;
