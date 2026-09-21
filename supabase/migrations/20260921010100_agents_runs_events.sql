-- Step 1: the execution tables. Agents, the runs they perform, the event log
-- every action writes to, and the per-call cost record.
--
-- Cross-table foreign keys are composite, on (id, org_id) rather than (id).
-- That makes it structurally impossible for a run to point at an agent in
-- another organisation: the database rejects it rather than trusting every
-- caller to check.

create table public.agents (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  name text not null,
  role text not null,
  parent_agent_id uuid,
  model_tier text not null default 'cheap'
    check (model_tier in ('cheap', 'standard', 'frontier')),
  config jsonb not null default '{}'::jsonb,
  daily_budget_usd numeric(10, 4) not null default 0
    check (daily_budget_usd >= 0),
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, name),
  unique (id, org_id),
  foreign key (parent_agent_id, org_id)
    references public.agents (id, org_id) on delete set null,
  constraint agents_no_self_parent check (parent_agent_id is null or parent_agent_id <> id)
);

comment on column public.agents.parent_agent_id is
  'Department head that owns this worker. Null for a top-level agent.';
comment on column public.agents.model_tier is
  'Resolved to a concrete model by the gateway, so retiering needs no code change.';

create index agents_org_parent_idx on public.agents (org_id, parent_agent_id);

create trigger agents_set_updated_at
  before update on public.agents
  for each row execute function public.set_updated_at();

create table public.runs (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  agent_id uuid not null,
  trigger text not null,
  status text not null default 'pending'
    check (status in ('pending', 'running', 'paused', 'succeeded', 'failed', 'cancelled')),
  started_at timestamptz,
  ended_at timestamptz,
  tokens_in bigint not null default 0 check (tokens_in >= 0),
  tokens_out bigint not null default 0 check (tokens_out >= 0),
  cost_usd numeric(12, 6) not null default 0 check (cost_usd >= 0),
  idempotency_key text not null,
  checkpoint_thread_id text,
  max_steps integer not null default 25 check (max_steps > 0),
  max_tokens integer not null default 100000 check (max_tokens > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, idempotency_key),
  unique (id, org_id),
  foreign key (agent_id, org_id)
    references public.agents (id, org_id) on delete cascade
);

-- Serverless retries re-deliver the same trigger. This constraint, not
-- application logic, is what makes a repeated trigger produce one run.
comment on constraint runs_org_id_idempotency_key_key on public.runs is
  'Enforces trigger idempotency under serverless re-delivery.';
comment on column public.runs.max_steps is
  'Per-run cap. No run may loop without a bound.';

create index runs_org_status_idx on public.runs (org_id, status);
create index runs_org_agent_started_idx on public.runs (org_id, agent_id, started_at desc);

create trigger runs_set_updated_at
  before update on public.runs
  for each row execute function public.set_updated_at();

-- Append-only from here down. Events, model calls and judgments are the audit
-- trail, so no update or delete policy is granted on them.
create table public.events (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  run_id uuid,
  agent_id uuid,
  type text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  foreign key (run_id, org_id) references public.runs (id, org_id) on delete cascade,
  foreign key (agent_id, org_id) references public.agents (id, org_id) on delete cascade
);

comment on table public.events is
  'Every agent action. Drives the live brain UI through Realtime and doubles '
  'as the audit log. Append-only: no update or delete policy is granted.';

create index events_org_created_idx on public.events (org_id, created_at desc);
create index events_org_run_idx on public.events (org_id, run_id);

create table public.model_calls (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  run_id uuid,
  agent_id uuid,
  model text not null,
  provider text,
  tokens_in bigint not null default 0 check (tokens_in >= 0),
  tokens_out bigint not null default 0 check (tokens_out >= 0),
  cost_usd numeric(12, 6) not null default 0 check (cost_usd >= 0),
  latency_ms integer check (latency_ms >= 0),
  created_at timestamptz not null default now(),
  foreign key (run_id, org_id) references public.runs (id, org_id) on delete cascade,
  foreign key (agent_id, org_id) references public.agents (id, org_id) on delete cascade
);

comment on table public.model_calls is
  'One row per model call, written by the gateway. The source of truth for '
  'per-agent budget enforcement and cost reporting.';

-- Budget checks read this by agent and day, on every call.
create index model_calls_org_agent_created_idx
  on public.model_calls (org_id, agent_id, created_at desc);

alter table public.agents enable row level security;
alter table public.runs enable row level security;
alter table public.events enable row level security;
alter table public.model_calls enable row level security;

create policy agents_select on public.agents
  for select to authenticated using (public.is_org_member(org_id));
create policy agents_insert on public.agents
  for insert to authenticated with check (public.is_org_member(org_id));
create policy agents_update on public.agents
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

create policy runs_select on public.runs
  for select to authenticated using (public.is_org_member(org_id));
create policy runs_insert on public.runs
  for insert to authenticated with check (public.is_org_member(org_id));
create policy runs_update on public.runs
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

create policy events_select on public.events
  for select to authenticated using (public.is_org_member(org_id));
create policy events_insert on public.events
  for insert to authenticated with check (public.is_org_member(org_id));

create policy model_calls_select on public.model_calls
  for select to authenticated using (public.is_org_member(org_id));
create policy model_calls_insert on public.model_calls
  for insert to authenticated with check (public.is_org_member(org_id));

grant select, insert, update on public.agents to authenticated;
grant select, insert, update on public.runs to authenticated;
grant select, insert on public.events to authenticated;
grant select, insert on public.model_calls to authenticated;

grant select, insert, update, delete on public.agents to service_role;
grant select, insert, update, delete on public.runs to service_role;
grant select, insert, update, delete on public.events to service_role;
grant select, insert, update, delete on public.model_calls to service_role;
