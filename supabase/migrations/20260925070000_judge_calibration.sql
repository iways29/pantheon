-- Step 5.4: labelled cases and eval results for calibrating the judge gates.
--
-- ADR 012. Thresholds are set from data, not from cookbook numbers. A case is
-- a state a gate might see and the outcome a person says is right. An eval
-- run asks the gate's live questions about every case and records how often
-- the gate's decision matched, what it cost and how long it took, so a later
-- model or threshold change can be compared with it.

create table public.judge_cases (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  gate text not null check (gate ~ '^[a-z][a-z0-9_]*$'),
  -- Stable name for the case, so re-importing a file updates it in place.
  case_key text not null check (case_key ~ '^[a-z0-9][a-z0-9_.:-]*$'),
  state jsonb not null,
  -- One of the gate's outcomes. Checked by the application, which knows them.
  expected text not null check (expected ~ '^[a-z][a-z0-9_]*$'),
  -- Where the label came from (open decision 14): the owner by hand, an
  -- approval decision, a frontier-model ensemble, or the case's author.
  label_source text not null
    check (label_source in ('owner', 'approval', 'ensemble', 'author')),
  -- Known Jev weak spots get their own line in the report: arithmetic,
  -- dates, double negatives, adversarial text.
  weak_spot text check (weak_spot in ('arithmetic', 'dates', 'double_negative', 'adversarial')),
  note text,
  active boolean not null default true,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, gate, case_key)
);

comment on table public.judge_cases is
  'Labelled examples per gate, for measuring precision and recall and setting '
  'thresholds from data (Step 5.4).';

create trigger judge_cases_set_updated_at
  before update on public.judge_cases
  for each row execute function public.set_updated_at();

create index judge_cases_org_gate_idx on public.judge_cases (org_id, gate) where active;

alter table public.judge_cases enable row level security;

create policy judge_cases_select on public.judge_cases
  for select to authenticated using (public.is_org_member(org_id));
create policy judge_cases_insert on public.judge_cases
  for insert to authenticated with check (public.is_org_member(org_id));
create policy judge_cases_update on public.judge_cases
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

grant select, insert, update on public.judge_cases to authenticated;
grant select, insert, update, delete on public.judge_cases to service_role;

create table public.judge_eval_runs (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  gate text not null,
  gate_version integer not null,
  model text not null,
  profile text,
  cases integer not null check (cases >= 0),
  repeats integer not null check (repeats >= 1),
  -- Accuracy, per-outcome precision and recall, confidence bins,
  -- consistency, weak spots, threshold sweeps and recommendations.
  metrics jsonb not null,
  tokens_in bigint not null default 0,
  cost_usd numeric(12, 6) not null default 0,
  latency_ms_p50 integer,
  latency_ms_p95 integer,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now()
);

comment on table public.judge_eval_runs is
  'One row per eval run of a gate over its labelled cases. The record behind '
  'every threshold change and model upgrade.';

create index judge_eval_runs_org_gate_idx on public.judge_eval_runs (org_id, gate, created_at desc);

alter table public.judge_eval_runs enable row level security;

create policy judge_eval_runs_select on public.judge_eval_runs
  for select to authenticated using (public.is_org_member(org_id));
create policy judge_eval_runs_insert on public.judge_eval_runs
  for insert to authenticated with check (public.is_org_member(org_id));

grant select, insert on public.judge_eval_runs to authenticated;
grant select, insert on public.judge_eval_runs to service_role;
