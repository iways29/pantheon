-- Step 1: the brain itself, plus the governance tables that decide what an
-- agent is allowed to do.

-- The fact house. Facts are never deleted when they turn out to be wrong:
-- they are superseded, so the trail of what was believed and when survives.
create table public.facts (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  claim text not null,
  source text,
  source_ref text,
  confidence numeric(4, 3) check (confidence >= 0 and confidence <= 1),
  status text not null default 'active'
    check (status in ('active', 'superseded', 'disputed')),
  superseded_by uuid,
  created_by_run_id uuid,
  embedding vector(1536),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, org_id),
  foreign key (superseded_by, org_id)
    references public.facts (id, org_id) on delete set null,
  foreign key (created_by_run_id, org_id)
    references public.runs (id, org_id) on delete set null,
  constraint facts_no_self_supersede
    check (superseded_by is null or superseded_by <> id),
  constraint facts_superseded_has_successor
    check (status <> 'superseded' or superseded_by is not null)
);

comment on column public.facts.embedding is
  '1536 dimensions. Changing this needs a migration and a re-embed, so the '
  'gateway must pin the embedding model to match.';
comment on constraint facts_superseded_has_successor on public.facts is
  'A fact marked superseded must say what replaced it.';

-- Cosine distance, matching the operator the brain module queries with.
create index facts_embedding_idx on public.facts
  using hnsw (embedding vector_cosine_ops);
create index facts_org_status_idx on public.facts (org_id, status);

create trigger facts_set_updated_at
  before update on public.facts
  for each row execute function public.set_updated_at();

-- Irreversible or outward-facing actions wait here for a human.
create table public.approvals (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  run_id uuid,
  action_type text not null,
  payload jsonb not null default '{}'::jsonb,
  agent_output_snapshot jsonb,
  status text not null default 'pending'
    check (status in ('pending', 'approved', 'rejected', 'expired')),
  verdict text,
  decided_at timestamptz,
  decided_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (run_id, org_id) references public.runs (id, org_id) on delete cascade,
  constraint approvals_decided_together
    check ((status = 'pending') = (decided_at is null))
);

comment on column public.approvals.agent_output_snapshot is
  'What the agent proposed, captured at request time. Each decision doubles '
  'as a labelled example for judge calibration, which is only possible if the '
  'output is frozen rather than re-read later.';

create index approvals_org_status_idx on public.approvals (org_id, status, created_at desc);

create trigger approvals_set_updated_at
  before update on public.approvals
  for each row execute function public.set_updated_at();

-- Raw judge output. Thresholds and policy live in config, never here, so a
-- policy change does not rewrite history.
create table public.judgments (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  run_id uuid,
  gate text not null,
  question_id text not null,
  question_version text not null,
  input_ref text,
  output jsonb not null,
  created_at timestamptz not null default now(),
  foreign key (run_id, org_id) references public.runs (id, org_id) on delete cascade
);

comment on table public.judgments is
  'Append-only record of raw probabilities. Confidence here is evidence, not '
  'permission to act.';

create index judgments_org_gate_idx on public.judgments (org_id, gate, created_at desc);

create table public.system_flags (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  key text not null,
  value jsonb not null default 'false'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, key)
);

create trigger system_flags_set_updated_at
  before update on public.system_flags
  for each row execute function public.set_updated_at();

-- Checked before every model call and every run start. Defaults to off when
-- the row is absent, but an explicit row is created with each org.
create or replace function public.kill_switch_on(target_org_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select coalesce(
    (select f.value = to_jsonb(true)
     from public.system_flags f
     where f.org_id = target_org_id and f.key = 'kill_switch'),
    false
  );
$$;

alter table public.facts enable row level security;
alter table public.approvals enable row level security;
alter table public.judgments enable row level security;
alter table public.system_flags enable row level security;

create policy facts_select on public.facts
  for select to authenticated using (public.is_org_member(org_id));
create policy facts_insert on public.facts
  for insert to authenticated with check (public.is_org_member(org_id));
create policy facts_update on public.facts
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

create policy approvals_select on public.approvals
  for select to authenticated using (public.is_org_member(org_id));
create policy approvals_insert on public.approvals
  for insert to authenticated with check (public.is_org_member(org_id));
create policy approvals_update on public.approvals
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

create policy judgments_select on public.judgments
  for select to authenticated using (public.is_org_member(org_id));
create policy judgments_insert on public.judgments
  for insert to authenticated with check (public.is_org_member(org_id));

create policy system_flags_select on public.system_flags
  for select to authenticated using (public.is_org_member(org_id));

grant select, insert, update on public.facts to authenticated;
grant select, insert, update on public.approvals to authenticated;
grant select, insert on public.judgments to authenticated;
grant select on public.system_flags to authenticated;
grant execute on function public.kill_switch_on(uuid) to authenticated;

grant select, insert, update, delete on public.facts to service_role;
grant select, insert, update, delete on public.approvals to service_role;
grant select, insert, update, delete on public.judgments to service_role;
grant select, insert, update, delete on public.system_flags to service_role;
