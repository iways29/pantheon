-- Step 9: Jev checks the workers' results; a failed check redoes the task
-- once on a stronger tier (owner, 2026-09-27; right-hand idea 7).
--
-- - `tasks.model_tier`: the tier the task's next run uses instead of its
--   agent's (null: the agent's). `tasks.escalations`: how many times it was
--   redone on a stronger tier. `runs.model_tier`: copied from the task when
--   the run is dispatched; the gateway uses it, upward only.
-- - Only the backend sets these. An agent (or anyone signed in) cannot move
--   work to a dearer model.
-- - A run that ends with stop reason `escalated` leaves its task alone: the
--   backend has already put the task back in the queue for its redo.

alter table public.tasks
  add column model_tier text check (model_tier in ('cheap', 'standard', 'frontier')),
  add column escalations integer not null default 0 check (escalations between 0 and 5);

alter table public.runs
  add column model_tier text check (model_tier in ('cheap', 'standard', 'frontier'));

comment on column public.tasks.model_tier is
  'The tier the next run uses instead of the agent''s, after a failed result check. '
  'Set by the backend only.';
comment on column public.runs.model_tier is
  'The tier this run uses instead of its agent''s (upward only). Set by the backend only.';

create or replace function public.tier_is_backend_only()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if current_user not in ('authenticated', 'anon') then
    return new;
  end if;
  if tg_table_name = 'tasks' then
    if (tg_op = 'INSERT' and (new.model_tier is not null or new.escalations <> 0))
       or (tg_op = 'UPDATE' and (new.model_tier is distinct from old.model_tier
                                 or new.escalations <> old.escalations)) then
      raise exception 'Only the backend changes a task''s tier'
        using errcode = 'insufficient_privilege';
    end if;
  elsif (tg_op = 'INSERT' and new.model_tier is not null)
        or (tg_op = 'UPDATE' and new.model_tier is distinct from old.model_tier) then
    raise exception 'Only the backend changes a run''s tier'
      using errcode = 'insufficient_privilege';
  end if;
  return new;
end;
$$;

revoke execute on function public.tier_is_backend_only() from public, anon, authenticated;

create trigger tasks_tier_is_backend_only
  before insert or update on public.tasks
  for each row execute function public.tier_is_backend_only();
create trigger runs_tier_is_backend_only
  before insert or update on public.runs
  for each row execute function public.tier_is_backend_only();

-- As before, and the run carries the task's tier.
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
       max_tokens, task_id, model_tier)
    values
      (t.org_id, t.assigned_agent_id, 'task', 'task:' || t.id || ':' || (t.attempts + 1),
       t.input || jsonb_build_object('task_id', t.id, 'title', t.title,
                                     'instructions', t.instructions),
       t.requested_by, t.max_steps, t.max_tokens, t.id, t.model_tier)
    on conflict (org_id, idempotency_key) do nothing
    returning id into new_run;
    update public.tasks set status = 'running', attempts = attempts + 1 where id = t.id;
    if new_run is not null then
      return next new_run;
    end if;
  end loop;
end;
$$;

revoke execute on function public.dispatch_queued_tasks(integer) from public, anon, authenticated;
grant execute on function public.dispatch_queued_tasks(integer) to service_role;

-- As before, except a run stopped for a redo on a stronger tier.
create or replace function public.task_follows_run()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  open_children integer;
begin
  if new.task_id is null or new.status = old.status or new.stop_reason = 'escalated' then
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
