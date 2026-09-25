-- Step 7.5: approvals that pause and resume agents, and the decision desk
-- (ADR 021; right-hand ideas 1, 2 and 4, docs/design/right-hand.md).
--
-- A held tool call pauses its run at that exact point (the checkpoint). The
-- owner approves (optionally editing the arguments), rejects and cancels, or
-- rejects with a note so the agent carries on without it. Approving resumes
-- the run: the tool call replays by its idempotency key and runs once, with
-- the approved arguments.

-- --- Tool calls can wait for, and carry, the owner's decision ----------------

alter table public.tool_calls
  drop constraint tool_calls_status_check,
  add constraint tool_calls_status_check
    check (status in ('ok', 'refused', 'error', 'held', 'approved', 'rejected'));

drop index public.tool_calls_org_idempotency_key;
create unique index tool_calls_org_idempotency_key
  on public.tool_calls (org_id, idempotency_key)
  where idempotency_key is not null and status in ('ok', 'held', 'approved', 'rejected');

grant update (status, arguments, result, error, latency_ms) on public.tool_calls to authenticated;
grant update (status, arguments, result, error, latency_ms) on public.tool_calls to service_role;
create policy tool_calls_update on public.tool_calls
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

-- --- The decision card, stored with the approval ------------------------------

alter table public.approvals
  add column agent_id uuid,
  add column task_id uuid,
  add column tool_call_id uuid,
  -- What the action is, for grouping agreement: e.g. tool:send_email.
  add column action_key text,
  -- Idea 1: Jev's recommendation, its probabilities and the judgment behind it.
  add column recommendation text check (recommendation in ('approve', 'reject', 'look_closer')),
  add column recommendation_probs jsonb,
  add column recommendation_request_id uuid,
  add column explanation text,
  add column facts_checked jsonb not null default '[]'::jsonb,
  add column similar_decisions jsonb not null default '[]'::jsonb,
  -- Idea 4: earlier owner decisions or policies this goes against.
  add column conflicts jsonb not null default '[]'::jsonb,
  -- What the owner changed before approving, if anything.
  add column edited_payload jsonb,
  add constraint approvals_agent_fkey foreign key (agent_id, org_id)
    references public.agents (id, org_id) on delete set null (agent_id),
  add constraint approvals_task_fkey foreign key (task_id, org_id)
    references public.tasks (id, org_id) on delete set null (task_id);

create index approvals_action_key_idx on public.approvals (org_id, action_key, decided_at desc);

-- Idea 2: how often the recommendation matched the owner, per kind of action.
create or replace view public.approval_agreement
with (security_invoker = true)
as
select org_id,
       action_key,
       count(*) filter (where status in ('approved', 'rejected')) as decided,
       count(*) filter (where recommendation is not null
                          and status in ('approved', 'rejected')) as recommended,
       count(*) filter (where (recommendation = 'approve' and status = 'approved')
                           or (recommendation = 'reject' and status = 'rejected')) as agreed,
       round(
         (count(*) filter (where (recommendation = 'approve' and status = 'approved')
                              or (recommendation = 'reject' and status = 'rejected')))::numeric
         / nullif(count(*) filter (where recommendation is not null
                                     and status in ('approved', 'rejected')), 0),
         4) as agreement
  from public.approvals
 where action_key is not null
 group by org_id, action_key;

grant select on public.approval_agreement to authenticated, service_role;

-- --- Deciding --------------------------------------------------------------------

-- The owner's decision, in one transaction: the approval, the held tool call,
-- the task and the run all move together, and the whole thing is an event.
-- SECURITY INVOKER: RLS decides who may; an agent's session never may.
create or replace function public.decide_approval(
  p_approval_id uuid,
  p_decision text,          -- approve | cancel | redirect
  p_note text default null,
  p_edited_arguments jsonb default null
)
returns public.approvals
language plpgsql
set search_path = ''
as $$
declare
  a public.approvals;
  run_id uuid;
begin
  if public.current_agent_id() is not null then
    raise exception 'Only a person may decide an approval' using errcode = 'insufficient_privilege';
  end if;
  if p_decision not in ('approve', 'cancel', 'redirect') then
    raise exception 'Unknown decision %', p_decision using errcode = 'check_violation';
  end if;

  select * into a from public.approvals where id = p_approval_id for update;
  if not found then
    raise exception 'No approval %', p_approval_id using errcode = 'no_data_found';
  end if;
  if a.status <> 'pending' then
    return a;  -- deciding twice changes nothing
  end if;

  update public.approvals
     set status = case when p_decision = 'approve' then 'approved' else 'rejected' end,
         verdict = coalesce(p_note, p_decision),
         decided_at = now(),
         decided_by = auth.uid(),
         edited_payload = p_edited_arguments
   where id = a.id
  returning * into a;

  if a.tool_call_id is not null then
    update public.tool_calls
       set status = case when p_decision = 'approve' then 'approved' else 'rejected' end,
           arguments = coalesce(p_edited_arguments, arguments),
           error = case when p_decision = 'approve' then null else coalesce(p_note, 'Rejected by the owner') end
     where id = a.tool_call_id and status = 'held';
  end if;

  select r.id into run_id from public.runs r
   where r.id = a.run_id and r.status = 'paused' and r.stop_reason = 'awaiting_approval';

  if p_decision = 'cancel' then
    if a.task_id is not null then
      update public.tasks set status = 'cancelled', finished_at = now()
       where id = a.task_id and status not in ('done', 'failed', 'cancelled');
    end if;
    if run_id is not null then
      update public.runs set status = 'cancelled', stop_reason = 'rejected', ended_at = now()
       where id = run_id;
    end if;
  else
    if a.task_id is not null then
      update public.tasks set status = 'running' where id = a.task_id and status = 'awaiting_approval';
    end if;
    if run_id is not null then
      -- The scheduler wakes it; it resumes from its checkpoint.
      update public.runs
         set stop_reason = case when p_decision = 'approve' then 'approved' else 'redirected' end,
             wake_count = 0, last_wake_at = null
       where id = run_id;
    end if;
  end if;

  insert into public.events (org_id, run_id, agent_id, type, payload)
  values (a.org_id, a.run_id, a.agent_id, 'approval_decided',
          jsonb_build_object('approval_id', a.id, 'decision', p_decision, 'note', p_note,
                             'recommendation', a.recommendation, 'action_key', a.action_key,
                             'edited', p_edited_arguments is not null,
                             'decided_by', auth.uid()));
  return a;
end;
$$;

revoke execute on function public.decide_approval(uuid, text, text, jsonb) from public, anon;
grant execute on function public.decide_approval(uuid, text, text, jsonb) to authenticated, service_role;

-- Runs paused for approval wake once decided, like runs paused at a deadline.
-- A run still pausing when the owner decided (so its stop reason was not yet
-- `awaiting_approval` to update) wakes once nothing it waits on is pending.
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
          or (x.status = 'paused' and x.stop_reason in ('deadline', 'approved', 'redirected'))
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

-- Every new approval is an event (the decision desk's feed).
create or replace function public.audit_approval_requested()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  insert into public.events (org_id, run_id, agent_id, type, payload)
  values (new.org_id, new.run_id, new.agent_id, 'approval_requested',
          jsonb_build_object('approval_id', new.id, 'action_type', new.action_type,
                             'action_key', new.action_key, 'task_id', new.task_id));
  return new;
end;
$$;

create trigger approvals_requested
  after insert on public.approvals
  for each row execute function public.audit_approval_requested();

-- Only a person decides. An agent's session may create an approval and may
-- record the result of a call the owner approved, but may never decide,
-- edit a decision, or move a held call on by itself.
create or replace function public.approvals_only_people_decide()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if public.current_agent_id() is null then
    return new;
  end if;
  if tg_table_name = 'approvals' then
    if (new.status, new.verdict, new.decided_at, new.decided_by, new.edited_payload)
       is distinct from (old.status, old.verdict, old.decided_at, old.decided_by, old.edited_payload) then
      raise exception 'Only a person may decide an approval' using errcode = 'insufficient_privilege';
    end if;
  elsif old.status = 'held'
        or (new.status in ('approved', 'rejected') and new.status is distinct from old.status)
        or (old.status in ('approved', 'rejected') and new.arguments is distinct from old.arguments) then
    raise exception 'Only a person may decide a held tool call' using errcode = 'insufficient_privilege';
  end if;
  return new;
end;
$$;

create trigger approvals_only_people_decide
  before update on public.approvals
  for each row execute function public.approvals_only_people_decide();

create trigger tool_calls_only_people_decide
  before update on public.tool_calls
  for each row execute function public.approvals_only_people_decide();
