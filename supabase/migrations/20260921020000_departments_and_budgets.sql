-- Departments own the budget.
--
-- The build plan put the daily budget on each agent. Moving it to a
-- department matches how the work is actually organised -- a head agent and
-- its workers share one pot -- and makes provider-side caps practical: one
-- OpenRouter key per department is a handful of keys, where one per agent
-- would be key sprawl nobody maintains.
--
-- Safe as a single migration because agents is still empty everywhere; the
-- NOT NULL below would need a backfill once it is not.

create table public.departments (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  name text not null,
  daily_budget_usd numeric(10, 4) not null default 0
    check (daily_budget_usd >= 0),
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, name),
  unique (id, org_id)
);

comment on table public.departments is
  'A budget holder. Agents belong to exactly one, and spend is enforced here.';
comment on column public.departments.daily_budget_usd is
  'Defaults to zero, which means nothing approved yet rather than unlimited. '
  'An unfunded department cannot spend.';
comment on column public.departments.enabled is
  'A department-wide off switch, narrower than the org kill switch.';

create trigger departments_set_updated_at
  before update on public.departments
  for each row execute function public.set_updated_at();

-- Composite again, so a department cannot be borrowed across organisations.
alter table public.agents
  add column department_id uuid not null,
  add constraint agents_department_id_org_id_fkey
    foreign key (department_id, org_id)
    references public.departments (id, org_id) on delete cascade;

create index agents_org_department_idx on public.agents (org_id, department_id);

-- The per-agent budget survives as an optional sub-cap rather than the main
-- control: useful for throttling one noisy worker inside an otherwise healthy
-- department. NULL now means "no sub-cap"; the department alone governs.
-- Per-agent *spend* is still tracked either way, through model_calls.agent_id.
alter table public.agents
  alter column daily_budget_usd drop default,
  alter column daily_budget_usd drop not null;

comment on column public.agents.daily_budget_usd is
  'Optional per-agent sub-cap. NULL means only the department budget applies. '
  'Zero means this agent specifically may not spend.';

alter table public.departments enable row level security;

create policy departments_select on public.departments
  for select to authenticated using (public.is_org_member(org_id));
create policy departments_insert on public.departments
  for insert to authenticated with check (public.is_org_member(org_id));
create policy departments_update on public.departments
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

grant select, insert, update on public.departments to authenticated;
grant select, insert, update, delete on public.departments to service_role;
