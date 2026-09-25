-- Step 7.6: the autonomy ladder and safety limits (ADR 022).
--
-- - The autonomy ladder: what an agent at each level may do alone, per tool
--   risk class, as owner-editable data with built-in defaults. R4 is always
--   held, whatever the data says.
-- - Promotion suggestions from approval history (right-hand idea 2): shown,
--   never applied. Only the owner changes `agents.autonomy_level`.
-- - More owner-set limits: tasks per agent per hour, the loop limit (the same
--   tool with the same arguments), when a task counts as stuck, and the bar
--   for suggesting a promotion.
-- - A stuck-task reaper, run every tick.
-- - The kill switch holds wake-ups as well as new runs and tasks. Runs it
--   paused stay paused after it is switched off until the owner resumes them
--   (ADR 008's rule), now with one call: resume_paused_runs().

-- --- More limits, one place ---------------------------------------------------

alter table public.delegation_limits
  add column max_tasks_per_agent_per_hour integer not null default 20
    check (max_tasks_per_agent_per_hour between 1 and 500),
  add column loop_repeat_limit integer not null default 3
    check (loop_repeat_limit between 2 and 10),
  add column stuck_task_minutes integer not null default 60
    check (stuck_task_minutes between 10 and 1440),
  add column promotion_min_decisions integer not null default 30
    check (promotion_min_decisions between 5 and 1000),
  add column promotion_min_agreement numeric(5, 4) not null default 0.95
    check (promotion_min_agreement between 0.5 and 1);

comment on table public.delegation_limits is
  'Owner-set limits on task trees and agents. No row means the defaults (org_limits).';

-- An org's limits, or the defaults when the owner has set none.
create or replace function public.org_limits(p_org_id uuid)
returns public.delegation_limits
language plpgsql
stable
set search_path = ''
as $$
declare
  l public.delegation_limits;
begin
  select * into l from public.delegation_limits where org_id = p_org_id;
  if not found then
    l.org_id := p_org_id;
    l.max_depth := 3;
    l.max_children := 4;
    l.max_tasks_per_department_per_day := 50;
    l.max_tasks_per_agent_per_hour := 20;
    l.loop_repeat_limit := 3;
    l.stuck_task_minutes := 60;
    l.promotion_min_decisions := 30;
    l.promotion_min_agreement := 0.95;
  end if;
  return l;
end;
$$;

revoke execute on function public.org_limits(uuid) from public, anon;
grant execute on function public.org_limits(uuid) to authenticated, service_role;

-- Changing an autonomy rule is an audited event.
create or replace function public.audit_limits_change()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  subject record;
begin
  if tg_op = 'DELETE' then
    if not exists (select 1 from public.orgs where id = old.org_id) then
      return old;
    end if;
    subject := old;
  else
    subject := new;
  end if;
  insert into public.events (org_id, type, payload)
  values (subject.org_id, tg_argv[0],
          jsonb_build_object('op', lower(tg_op), 'row', to_jsonb(subject),
                             'changed_by', auth.uid(), 'db_role', current_user));
  return subject;
end;
$$;

-- The limits audit (ADR 019) records every column, the new ones included.
create or replace function public.audit_delegation_limits()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'delegation_limits_changed',
          (to_jsonb(new) - 'org_id' - 'updated_at')
          || jsonb_build_object('changed_by', auth.uid(), 'db_role', current_user));
  return new;
end;
$$;

-- --- The autonomy ladder --------------------------------------------------------

-- What an agent at `level` does with a tool of `risk_class`:
--   run:  runs (still validated, logged, screened)
--   gate: the tool-risk gate decides: run, hold for the owner, or refuse
--   hold: always waits for the owner
-- Rows override the defaults in tool_mode(). R4 is not configurable.
create table public.autonomy_rules (
  org_id uuid not null references public.orgs (id) on delete cascade,
  level text not null check (level in ('L0', 'L1', 'L2', 'L3')),
  risk_class text not null check (risk_class in ('R0', 'R1', 'R2', 'R3')),
  mode text not null check (mode in ('run', 'gate', 'hold')),
  updated_at timestamptz not null default now(),
  primary key (org_id, level, risk_class)
);

create trigger autonomy_rules_set_updated_at
  before update on public.autonomy_rules
  for each row execute function public.set_updated_at();

create trigger autonomy_rules_audit
  after insert or update or delete on public.autonomy_rules
  for each row execute function public.audit_limits_change('autonomy_rule_changed');

alter table public.autonomy_rules enable row level security;
create policy autonomy_rules_select on public.autonomy_rules
  for select to authenticated using (public.is_org_member(org_id));
-- Only a person sets autonomy; never an agent's session.
create policy autonomy_rules_write on public.autonomy_rules
  for all to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null)
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
grant select, insert, update, delete on public.autonomy_rules to authenticated;
grant select, insert, update, delete on public.autonomy_rules to service_role;

create or replace function public.tool_mode(p_org_id uuid, p_level text, p_risk_class text)
returns text
language plpgsql
stable
set search_path = ''
as $$
declare
  found_mode text;
begin
  if p_risk_class not in ('R0', 'R1', 'R2', 'R3') then
    return 'hold';  -- R4 (and anything unknown) always waits for the owner
  end if;
  select mode into found_mode from public.autonomy_rules
   where org_id = p_org_id and level = p_level and risk_class = p_risk_class;
  if found then
    return found_mode;
  end if;
  return case
    -- L0 drafts only: it reads internal data and asks for everything else.
    when p_level = 'L0' then case when p_risk_class = 'R0' then 'run' else 'hold' end
    -- L1, the default: internal work runs, outside reads are checked,
    -- outside effects wait.
    when p_level = 'L1' then case p_risk_class when 'R2' then 'gate' when 'R3' then 'hold'
                                                else 'run' end
    -- L2: outside effects are checked instead of always waiting.
    when p_level = 'L2' then case when p_risk_class in ('R2', 'R3') then 'gate' else 'run' end
    -- L3: outside reads run (still screened); outside effects are checked.
    else case when p_risk_class = 'R3' then 'gate' else 'run' end
  end;
end;
$$;

revoke execute on function public.tool_mode(uuid, text, text) from public, anon;
grant execute on function public.tool_mode(uuid, text, text) to authenticated, service_role;

-- --- Promotion suggestions (right-hand idea 2) ------------------------------------

-- Per agent and kind of action: how often Jev's recommendation matched the
-- owner, and whether that clears the owner's bar for suggesting the next
-- level. Irreversible (R4) and non-tool actions are never eligible.
create or replace view public.autonomy_suggestions
with (security_invoker = true)
as
with per as (
  select ap.org_id, ap.agent_id, ap.action_key,
         count(*) filter (where ap.recommendation is not null
                            and ap.status in ('approved', 'rejected')) as recommended,
         count(*) filter (where (ap.recommendation = 'approve' and ap.status = 'approved')
                             or (ap.recommendation = 'reject' and ap.status = 'rejected')) as agreed
    from public.approvals ap
   where ap.agent_id is not null and ap.action_key is not null
   group by ap.org_id, ap.agent_id, ap.action_key
)
select per.org_id,
       per.agent_id,
       a.name as agent,
       per.action_key,
       t.risk_class,
       per.recommended,
       per.agreed,
       round(per.agreed::numeric / nullif(per.recommended, 0), 4) as agreement,
       a.autonomy_level as current_level,
       case a.autonomy_level when 'L0' then 'L1' when 'L1' then 'L2' when 'L2' then 'L3' end
         as suggested_level,
       (per.recommended >= lim.promotion_min_decisions
        and per.agreed::numeric / nullif(per.recommended, 0) >= lim.promotion_min_agreement
        and coalesce(t.risk_class, 'R4') <> 'R4'
        and a.autonomy_level <> 'L3') as eligible
  from per
  join public.agents a on a.id = per.agent_id and a.org_id = per.org_id
  left join public.tools t on t.org_id = per.org_id and 'tool:' || t.name = per.action_key
  cross join lateral public.org_limits(per.org_id) lim;

grant select on public.autonomy_suggestions to authenticated, service_role;

-- --- Tasks per agent per hour ------------------------------------------------------

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
  recent integer;
  committed numeric;
begin
  limits := public.org_limits(new.org_id);

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
    select count(*) into recent from public.tasks
     where created_by_agent_id = new.created_by_agent_id
       and created_at >= now() - interval '1 hour';
    if recent >= limits.max_tasks_per_agent_per_hour then
      raise exception 'Agent has created % tasks in the last hour (the limit)', recent
        using errcode = 'check_violation';
    end if;
  end if;

  select count(*) into recent from public.tasks
   where assigned_agent_id = new.assigned_agent_id
     and created_at >= now() - interval '1 hour';
  if recent >= limits.max_tasks_per_agent_per_hour then
    raise exception 'Agent has been given % tasks in the last hour (the limit)', recent
      using errcode = 'check_violation';
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

-- --- The kill switch holds wake-ups too ------------------------------------------

-- While the switch is on nothing wakes (a wake would only pause again, and
-- spend one of the run's five wakes). A run the owner resumed (below) wakes
-- and carries on from its checkpoint.
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
              and x.stop_reason in ('deadline', 'approved', 'redirected', 'resumed'))
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

-- The owner resumes runs paused by the kill switch or a budget, once lifted.
-- They wake on the next tick and carry on from their checkpoints.
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

-- --- The stuck-task reaper ------------------------------------------------------------

-- A running task is stuck when, for longer than the org's `stuck_task_minutes`,
-- no run of it can make progress: none is pending or leased, none has wakes
-- left, and none is waiting on a person or a switch (approval, kill switch,
-- budget, a disabled agent or department, a missing prompt). Its runs are
-- failed as `stuck`, which fails the task and wakes its parent to see why.
create or replace function public.reap_stuck_tasks(
  p_limit integer default 20, p_now timestamptz default now()
)
returns setof uuid
language plpgsql
set search_path = ''
as $$
declare
  t record;
begin
  for t in
    select tk.id, tk.org_id, tk.assigned_agent_id, tk.title
      from public.tasks tk
     where tk.status = 'running'
       and tk.updated_at < p_now - make_interval(
             mins => (public.org_limits(tk.org_id)).stuck_task_minutes)
       and not exists (
         select 1 from public.runs r
          where r.task_id = tk.id
            and (
              (r.status = 'running' and r.lease_expires_at >= p_now)
              or (r.status in ('pending', 'running', 'paused') and r.wake_count < 5
                  and coalesce(r.stop_reason, '') not in ('upstream_error', 'error'))
              or (r.status = 'paused' and r.stop_reason in
                   ('awaiting_approval', 'kill_switch', 'budget_exceeded', 'agent_disabled',
                    'department_disabled', 'prompt_missing', 'resumed'))
            ))
     order by tk.updated_at
     limit p_limit
       for update of tk skip locked
  loop
    update public.runs
       set status = 'failed', stop_reason = 'stuck', ended_at = now(),
           error = 'No progress for too long; stopped by the reaper', lease_expires_at = null
     where task_id = t.id and status not in ('succeeded', 'failed', 'cancelled');
    update public.tasks
       set status = 'failed', error = 'stuck', finished_at = now()
     where id = t.id and status = 'running';
    insert into public.events (org_id, agent_id, type, payload)
    values (t.org_id, t.assigned_agent_id, 'task_reaped',
            jsonb_build_object('task_id', t.id, 'title', t.title));
    return next t.id;
  end loop;
end;
$$;

revoke execute on function public.reap_stuck_tasks(integer, timestamptz) from public, anon, authenticated;
grant execute on function public.reap_stuck_tasks(integer, timestamptz) to service_role;

create or replace function public.pantheon_tick()
returns void
language plpgsql
set search_path = ''
as $$
begin
  perform public.reap_stuck_tasks();
  perform public.dispatch_due_triggers();
  perform public.dispatch_queued_tasks();
  perform public.push_runs();
end;
$$;

revoke execute on function public.pantheon_tick() from public, anon, authenticated, service_role;
