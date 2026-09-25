-- Step 7.4: what runs an agent, and how much it may do alone (ADR 020).
--
-- An agent is a row plus a runner (docs/design/agent-organization.md
-- section 4). Code provides the runners; this says which one each agent uses.

alter table public.agents
  -- pipeline: a fixed LangGraph graph (the default, cheapest)
  -- deep: a deepagents tool loop, for open-ended work and heads
  -- router, digest: Jev routing and daily summaries (built in Step 8.2)
  add column runner text not null default 'pipeline'
    check (runner in ('pipeline', 'deep', 'router', 'digest')),
  -- L0 draft only, L1 default, L2, L3. Set only by the owner (Step 7.6).
  add column autonomy_level text not null default 'L1'
    check (autonomy_level in ('L0', 'L1', 'L2', 'L3')),
  -- A tighter cap on this agent's sub-tasks than the org limit. Null: the
  -- org limit applies.
  add column max_children integer check (max_children between 1 and 20);

comment on column public.agents.runner is
  'The code that runs this agent: pipeline, deep, router or digest.';

-- The audit trigger (ADR 014) also watches the new columns.
create or replace function public.audit_agent_change()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  event_type text;
  subject public.agents;
begin
  if tg_op = 'DELETE' then
    if not exists (select 1 from public.orgs where id = old.org_id) then
      return old;
    end if;
    subject := old;
    event_type := 'agent_deleted';
  elsif tg_op = 'INSERT' then
    subject := new;
    event_type := 'agent_created';
  else
    subject := new;
    if new.enabled and not old.enabled then
      event_type := 'agent_enabled';
    elsif old.enabled and not new.enabled then
      event_type := 'agent_disabled';
    elsif (new.name, new.role, new.department_id, new.parent_agent_id, new.model_tier,
           new.daily_budget_usd, new.allowed_tools, new.config, new.role_type, new.runner,
           new.autonomy_level, new.max_children)
          is distinct from
          (old.name, old.role, old.department_id, old.parent_agent_id, old.model_tier,
           old.daily_budget_usd, old.allowed_tools, old.config, old.role_type, old.runner,
           old.autonomy_level, old.max_children) then
      event_type := 'agent_updated';
    else
      return new;
    end if;
  end if;

  insert into public.events (org_id, agent_id, type, payload)
  values (
    subject.org_id,
    case when tg_op = 'DELETE' then null else subject.id end,
    event_type,
    jsonb_build_object(
      'agent_id', subject.id,
      'name', subject.name,
      'role', subject.role,
      'role_type', subject.role_type,
      'runner', subject.runner,
      'autonomy_level', subject.autonomy_level,
      'department_id', subject.department_id,
      'model_tier', subject.model_tier,
      'daily_budget_usd', subject.daily_budget_usd,
      'allowed_tools', to_jsonb(subject.allowed_tools),
      'enabled', subject.enabled,
      'changed_by', auth.uid(),
      'db_role', current_user
    )
  );
  return subject;
end;
$$;

-- The task guard (ADR 019) also honours an agent's own `max_children`.
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
  own_cap integer;
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
    select role_type, max_children into creator_role, own_cap
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
    if siblings >= least(limits.max_children, coalesce(own_cap, limits.max_children)) then
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
