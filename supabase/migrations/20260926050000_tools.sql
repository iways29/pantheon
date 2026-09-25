-- Step 7.2: tools as data, and a log of every tool call (ADR 018).
--
-- Code implements each tool; this table is how the owner configures it: its
-- description (what the model reads when choosing), how risky it is, whether
-- it needs approval, and whether it is switched on. Which agent may call
-- which tool is `agents.allowed_tools` (Step 6, audited by `agents_audit`).
--
-- Risk classes (docs/design/agent-organization.md section 5):
--   R0 read internal data          R3 reversible outside effect
--   R1 write internal data         R4 irreversible outside effect
--   R2 read the outside world      R5 never built (money, credentials)

create table public.tools (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  name text not null check (name ~ '^[a-z][a-z0-9_]*$'),
  description text not null check (btrim(description) <> ''),
  -- R5 is refused here as well as in code: such tools are never built.
  risk_class text not null check (risk_class in ('R0', 'R1', 'R2', 'R3', 'R4')),
  -- auto: runs; approval: always held for the owner. R4 is always held,
  -- whatever this says (the tool-risk gate, Step 7.5, may only tighten).
  approval text not null default 'auto' check (approval in ('auto', 'approval')),
  enabled boolean not null default true,
  timeout_seconds integer not null default 30 check (timeout_seconds between 1 and 300),
  max_output_chars integer not null default 8000 check (max_output_chars between 100 and 100000),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, name),
  constraint tools_r4_needs_approval check (risk_class <> 'R4' or approval = 'approval')
);

comment on table public.tools is
  'Per-org configuration of each tool code provides: description, risk class, '
  'approval policy, limits, on or off. Every change is written to events.';

create trigger tools_set_updated_at
  before update on public.tools
  for each row execute function public.set_updated_at();

create or replace function public.audit_tool_change()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  subject public.tools;
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
  values (
    subject.org_id,
    case tg_op when 'INSERT' then 'tool_created' when 'UPDATE' then 'tool_updated'
               else 'tool_deleted' end,
    jsonb_build_object(
      'tool', subject.name,
      'risk_class', subject.risk_class,
      'approval', subject.approval,
      'enabled', subject.enabled,
      'changed_by', auth.uid(),
      'db_role', current_user
    )
  );
  return subject;
end;
$$;

create trigger tools_audit
  after insert or update or delete on public.tools
  for each row execute function public.audit_tool_change();

alter table public.tools enable row level security;

create policy tools_select on public.tools
  for select to authenticated using (public.is_org_member(org_id));
-- Only a person configures tools; an agent session cannot.
create policy tools_insert on public.tools
  for insert to authenticated
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
create policy tools_update on public.tools
  for update to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null)
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);

grant select, insert, update on public.tools to authenticated;
grant select, insert, update, delete on public.tools to service_role;

-- Every tool call, allowed or not. The idempotency key makes a side-effecting
-- call act once: a repeat with the same key returns the stored result.
create table public.tool_calls (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  agent_id uuid not null,
  run_id uuid,
  tool text not null,
  idempotency_key text,
  arguments jsonb not null default '{}'::jsonb,
  status text not null check (status in ('ok', 'refused', 'error', 'held')),
  result jsonb,
  error text,
  latency_ms integer check (latency_ms >= 0),
  created_at timestamptz not null default now(),
  foreign key (agent_id, org_id) references public.agents (id, org_id) on delete cascade,
  foreign key (run_id, org_id) references public.runs (id, org_id) on delete cascade
);

create unique index tool_calls_org_idempotency_key
  on public.tool_calls (org_id, idempotency_key)
  where idempotency_key is not null and status in ('ok', 'held');
create index tool_calls_run_idx on public.tool_calls (run_id, created_at);

alter table public.tool_calls enable row level security;

create policy tool_calls_select on public.tool_calls
  for select to authenticated using (public.is_org_member(org_id));
create policy tool_calls_insert on public.tool_calls
  for insert to authenticated with check (public.is_org_member(org_id));

grant select, insert on public.tool_calls to authenticated;
grant select, insert on public.tool_calls to service_role;
