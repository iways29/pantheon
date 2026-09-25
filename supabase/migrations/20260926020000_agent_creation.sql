-- Step 6: agents are created from data, start switched off, and every change
-- is audited (ADR 014).
--
-- The owner creates agents through the API (and later the Control Center),
-- never by editing code. A new agent does nothing until the owner reviews and
-- enables it, so the column default flips to false. Existing agents keep the
-- state they have.

alter table public.agents
  alter column enabled set default false,
  -- The tools this agent may call, by name. The tool registry (Step 7.2) is
  -- the list of what exists; this is what the owner allows.
  add column allowed_tools text[] not null default '{}'
    check (array_position(allowed_tools, null) is null),
  add column created_by uuid default auth.uid();

comment on column public.agents.enabled is
  'Off by default: a new agent does nothing until the owner enables it.';
comment on column public.agents.allowed_tools is
  'Tool names the owner allows this agent to call. Empty: no tools.';

-- Every change to what an agent is or may do is an operational event. A
-- trigger, so a hand edit in the SQL editor is audited too.
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
    -- An org being deleted cascades here; there is nothing left to audit.
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
           new.daily_budget_usd, new.allowed_tools, new.config)
          is distinct from
          (old.name, old.role, old.department_id, old.parent_agent_id, old.model_tier,
           old.daily_budget_usd, old.allowed_tools, old.config) then
      event_type := 'agent_updated';
    else
      return new;
    end if;
  end if;

  insert into public.events (org_id, agent_id, type, payload)
  values (
    subject.org_id,
    -- A deleted agent's id cannot be referenced; it stays in the payload.
    case when tg_op = 'DELETE' then null else subject.id end,
    event_type,
    jsonb_build_object(
      'agent_id', subject.id,
      'name', subject.name,
      'role', subject.role,
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

create trigger agents_audit
  after insert or update or delete on public.agents
  for each row execute function public.audit_agent_change();
