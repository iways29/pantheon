-- Step 5.1: the judge core. TypeSafe Jev gates and questions as data.
--
-- ADR 009. A gate is a named decision point ("may this fact enter the
-- brain?"). It asks a set of typed questions in one TypeSafe request and turns
-- the raw answers into an outcome with thresholds held here, not in code. The
-- wording of a question is the only tuning surface Jev has, so it is
-- configuration the owner edits, versioned and audited like prompts (ADR 007).
--
-- Four changes:
--   model_prices     what a provider charges, so the gateway can cost a call
--                    that does not report its own price (TypeSafe does not)
--   judge_questions  one row per gate, question key and version
--   judge_gates      one row per gate and version: model, policy, fail mode
--   judgments        extra columns so every raw answer names its request,
--                    gate version, agent, model, tokens and latency

-- ---------------------------------------------------------------------------
-- Model prices
-- ---------------------------------------------------------------------------

-- OpenRouter reports the charge for each request (usage.cost), so its calls
-- need no table. TypeSafe reports tokens only. Cost is priority one, so the
-- price is data the owner can correct without a deploy, and a call to a model
-- with no price here is refused rather than logged at zero.
create table public.model_prices (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  provider text not null check (provider ~ '^[a-z][a-z0-9_-]*$'),
  model text not null check (model <> '' and model = btrim(model)),
  input_usd_per_mtok numeric(12, 6) not null check (input_usd_per_mtok >= 0),
  output_usd_per_mtok numeric(12, 6) not null default 0 check (output_usd_per_mtok >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, provider, model)
);

comment on table public.model_prices is
  'USD per million tokens for providers that do not report a per-request cost. '
  'Read by the gateway on every such call; every change is written to events.';

create trigger model_prices_set_updated_at
  before update on public.model_prices
  for each row execute function public.set_updated_at();

create or replace function public.audit_model_price_change()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  subject public.model_prices;
begin
  if tg_op = 'DELETE' then
    subject := old;
  else
    subject := new;
  end if;

  if tg_op = 'UPDATE'
     and (old.input_usd_per_mtok, old.output_usd_per_mtok)
         = (new.input_usd_per_mtok, new.output_usd_per_mtok) then
    return new;
  end if;

  insert into public.events (org_id, type, payload)
  values (
    subject.org_id,
    'model_price_changed',
    jsonb_build_object(
      'provider', subject.provider,
      'model', subject.model,
      'from', case when tg_op = 'INSERT' then null else jsonb_build_object(
        'input_usd_per_mtok', old.input_usd_per_mtok,
        'output_usd_per_mtok', old.output_usd_per_mtok) end,
      'to', case when tg_op = 'DELETE' then null else jsonb_build_object(
        'input_usd_per_mtok', new.input_usd_per_mtok,
        'output_usd_per_mtok', new.output_usd_per_mtok) end,
      'operation', lower(tg_op),
      'changed_by', auth.uid(),
      'db_role', current_user
    )
  );
  return subject;
end;
$$;

create trigger model_prices_audit
  after insert or update or delete on public.model_prices
  for each row execute function public.audit_model_price_change();

alter table public.model_prices enable row level security;

create policy model_prices_select on public.model_prices
  for select to authenticated using (public.is_org_member(org_id));
create policy model_prices_insert on public.model_prices
  for insert to authenticated with check (public.is_org_member(org_id));
create policy model_prices_update on public.model_prices
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));
create policy model_prices_delete on public.model_prices
  for delete to authenticated using (public.is_org_member(org_id));

grant select, insert, update, delete on public.model_prices to authenticated;
grant select, insert, update, delete on public.model_prices to service_role;

-- Jev 1.13's published price (docs.typesafe.ai/models, read 2026-09-25):
-- $0.042 per million input tokens, output tokens free.
insert into public.model_prices (org_id, provider, model, input_usd_per_mtok, output_usd_per_mtok)
select o.id, 'typesafe', 'jev-1.13.0', 0.042, 0
  from public.orgs o
on conflict (org_id, provider, model) do nothing;

-- ---------------------------------------------------------------------------
-- Judge questions
-- ---------------------------------------------------------------------------

-- The criteria each question type accepts, as TypeSafe documents them. A
-- function because a CHECK constraint cannot hold a subquery.
create or replace function public.judge_criteria_valid(p_type text, p_criteria jsonb)
returns boolean
language sql
immutable
set search_path = ''
as $$
  select case p_type
    when 'noul' then p_criteria is null or (
      jsonb_typeof(p_criteria) = 'object'
      and not exists (
        select 1 from jsonb_object_keys(p_criteria) k where k not in ('true', 'false')
      )
    )
    when 'choice' then coalesce(
      jsonb_typeof(p_criteria) = 'object'
      and (select count(*) from jsonb_object_keys(p_criteria)) between 1 and 255,
      false)
    when 'score' then coalesce(
      jsonb_typeof(p_criteria) = 'array'
      and jsonb_array_length(p_criteria) between 2 and 10,
      false)
    else false
  end;
$$;

create table public.judge_questions (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  gate text not null check (gate ~ '^[a-z][a-z0-9_]*$'),
  -- The key the answer comes back under. Never sent to the model, so the
  -- instructions must carry the whole meaning.
  key text not null check (key ~ '^[a-z][a-z0-9_]*$'),
  version integer not null check (version > 0),
  type text not null check (type in ('noul', 'choice', 'score')),
  -- A string, object or array, exactly as TypeSafe accepts it.
  instructions jsonb not null,
  -- Noul: optional {"true": ..., "false": ...}. Choice: required map of
  -- option to description (1 to 255 options). Score: required ordered array
  -- of level descriptions (2 to 10 levels).
  criteria jsonb,
  note text,
  active boolean not null default false,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now(),
  unique (org_id, gate, key, version),
  constraint judge_questions_instructions_present check (
    (jsonb_typeof(instructions) = 'string' and btrim(instructions #>> '{}') <> '')
    or (jsonb_typeof(instructions) in ('object', 'array') and instructions <> '{}'::jsonb
        and instructions <> '[]'::jsonb)
  ),
  constraint judge_questions_criteria_shape
    check (public.judge_criteria_valid(type, criteria))
);

comment on table public.judge_questions is
  'Versioned TypeSafe questions per gate. Append-only apart from the active '
  'flag; each activation and deactivation is written to events.';

-- At most one live version of each question in a gate. A gate's question set
-- is its active rows.
create unique index judge_questions_one_active
  on public.judge_questions (org_id, gate, key) where active;

create or replace function public.judge_questions_freeze_history()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if (new.id, new.org_id, new.gate, new.key, new.version, new.type, new.instructions,
      new.criteria, new.note, new.created_by, new.created_at)
     is distinct from
     (old.id, old.org_id, old.gate, old.key, old.version, old.type, old.instructions,
      old.criteria, old.note, old.created_by, old.created_at)
  then
    raise exception 'A published judge question cannot be edited; publish a new version'
      using errcode = 'restrict_violation';
  end if;
  return new;
end;
$$;

create trigger judge_questions_freeze
  before update on public.judge_questions
  for each row execute function public.judge_questions_freeze_history();

create or replace function public.audit_judge_question_change()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  event_type text;
begin
  if tg_op = 'INSERT' then
    if not new.active then
      return new;
    end if;
    event_type := 'judge_question_activated';
  elsif new.active and not old.active then
    event_type := 'judge_question_activated';
  elsif old.active and not new.active then
    event_type := 'judge_question_deactivated';
  else
    return new;
  end if;

  insert into public.events (org_id, type, payload)
  values (
    new.org_id,
    event_type,
    jsonb_build_object(
      'gate', new.gate,
      'key', new.key,
      'version', new.version,
      'changed_by', auth.uid(),
      'db_role', current_user
    )
  );
  return new;
end;
$$;

create trigger judge_questions_audit
  after insert or update of active on public.judge_questions
  for each row execute function public.audit_judge_question_change();

alter table public.judge_questions enable row level security;

create policy judge_questions_select on public.judge_questions
  for select to authenticated using (public.is_org_member(org_id));
create policy judge_questions_insert on public.judge_questions
  for insert to authenticated with check (public.is_org_member(org_id));
create policy judge_questions_update on public.judge_questions
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

grant select, insert on public.judge_questions to authenticated;
grant update (active) on public.judge_questions to authenticated;
grant select, insert on public.judge_questions to service_role;
grant update (active) on public.judge_questions to service_role;

-- Publish a new version of a question and make it live. SECURITY INVOKER, so
-- RLS decides whether the caller may write to this org.
create or replace function public.publish_judge_question(
  p_org_id uuid,
  p_gate text,
  p_key text,
  p_type text,
  p_instructions jsonb,
  p_criteria jsonb default null,
  p_note text default null
)
returns public.judge_questions
language plpgsql
set search_path = ''
as $$
declare
  v_next integer;
  result public.judge_questions;
begin
  perform pg_advisory_xact_lock(
    hashtextextended('judge_question:' || p_org_id::text || ':' || p_gate || ':' || p_key, 0));

  select coalesce(max(version), 0) + 1 into v_next
    from public.judge_questions
   where org_id = p_org_id and gate = p_gate and key = p_key;

  update public.judge_questions set active = false
   where org_id = p_org_id and gate = p_gate and key = p_key and active;

  insert into public.judge_questions
    (org_id, gate, key, version, type, instructions, criteria, note, active)
  values (p_org_id, p_gate, p_key, v_next, p_type, p_instructions, p_criteria, p_note, true)
  returning * into result;
  return result;
end;
$$;

-- Make an existing version live (rollback), or with p_version null take the
-- question out of the gate altogether (retire). Retiring keeps the history.
create or replace function public.activate_judge_question(
  p_org_id uuid,
  p_gate text,
  p_key text,
  p_version integer
)
returns public.judge_questions
language plpgsql
set search_path = ''
as $$
declare
  result public.judge_questions;
begin
  perform pg_advisory_xact_lock(
    hashtextextended('judge_question:' || p_org_id::text || ':' || p_gate || ':' || p_key, 0));

  if p_version is not null and not exists (
    select 1 from public.judge_questions
     where org_id = p_org_id and gate = p_gate and key = p_key and version = p_version
  ) then
    raise exception 'Gate "%" has no version % of question "%"', p_gate, p_version, p_key
      using errcode = 'no_data_found';
  end if;

  update public.judge_questions set active = false
   where org_id = p_org_id and gate = p_gate and key = p_key and active
     and version is distinct from p_version;

  if p_version is null then
    return null;
  end if;

  update public.judge_questions set active = true
   where org_id = p_org_id and gate = p_gate and key = p_key and version = p_version
  returning * into result;
  return result;
end;
$$;

revoke execute on function public.publish_judge_question(uuid, text, text, text, jsonb, jsonb, text)
  from public, anon;
revoke execute on function public.activate_judge_question(uuid, text, text, integer)
  from public, anon;
grant execute on function public.publish_judge_question(uuid, text, text, text, jsonb, jsonb, text)
  to authenticated, service_role;
grant execute on function public.activate_judge_question(uuid, text, text, integer)
  to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- Judge gates
-- ---------------------------------------------------------------------------

create table public.judge_gates (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  gate text not null check (gate ~ '^[a-z][a-z0-9_]*$'),
  version integer not null check (version > 0),
  -- A disabled gate refuses to judge. It never waves things through.
  enabled boolean not null default true,
  -- A pinned model version. Aliases such as jev-latest move when TypeSafe
  -- ships, which would change answers with no signal here (same rule as
  -- ADR 003). Move only after the calibration set passes (Step 5.4).
  model text not null
    check (model <> '' and model = btrim(model) and model !~ '-(latest|preview)$'),
  -- What happens when TypeSafe cannot answer (down, timeout, rate limited,
  -- malformed answer): 'closed' takes the most severe outcome, 'open' the
  -- least severe. Closed is the default and is right for brain writes and
  -- anything irreversible.
  fail_mode text not null default 'closed' check (fail_mode in ('open', 'closed')),
  -- Open decision 11: nothing marked sensitive goes to TypeSafe until it
  -- confirms request retention in writing.
  allow_sensitive boolean not null default false,
  -- The state is filtered in code first; this is the backstop, in characters.
  max_state_chars integer not null default 20000
    check (max_state_chars between 1 and 120000),
  -- Outcomes (least to most severe) and threshold rules. Shape is validated by
  -- the application (app/judge/policy.py) when published and when loaded.
  policy jsonb not null check (jsonb_typeof(policy) = 'object'),
  note text,
  active boolean not null default false,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now(),
  unique (org_id, gate, version)
);

comment on table public.judge_gates is
  'Versioned gate configuration: pinned model, thresholds, fail mode, '
  'sensitive-data switch. Append-only apart from the active flag; each '
  'activation and deactivation is written to events.';

create unique index judge_gates_one_active
  on public.judge_gates (org_id, gate) where active;

create or replace function public.judge_gates_freeze_history()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if (new.id, new.org_id, new.gate, new.version, new.enabled, new.model, new.fail_mode,
      new.allow_sensitive, new.max_state_chars, new.policy, new.note, new.created_by,
      new.created_at)
     is distinct from
     (old.id, old.org_id, old.gate, old.version, old.enabled, old.model, old.fail_mode,
      old.allow_sensitive, old.max_state_chars, old.policy, old.note, old.created_by,
      old.created_at)
  then
    raise exception 'A published gate version cannot be edited; publish a new version'
      using errcode = 'restrict_violation';
  end if;
  return new;
end;
$$;

create trigger judge_gates_freeze
  before update on public.judge_gates
  for each row execute function public.judge_gates_freeze_history();

create or replace function public.audit_judge_gate_change()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  event_type text;
begin
  if tg_op = 'INSERT' then
    if not new.active then
      return new;
    end if;
    event_type := 'judge_gate_activated';
  elsif new.active and not old.active then
    event_type := 'judge_gate_activated';
  elsif old.active and not new.active then
    event_type := 'judge_gate_deactivated';
  else
    return new;
  end if;

  insert into public.events (org_id, type, payload)
  values (
    new.org_id,
    event_type,
    jsonb_build_object(
      'gate', new.gate,
      'version', new.version,
      'enabled', new.enabled,
      'model', new.model,
      'fail_mode', new.fail_mode,
      'allow_sensitive', new.allow_sensitive,
      'changed_by', auth.uid(),
      'db_role', current_user
    )
  );
  return new;
end;
$$;

create trigger judge_gates_audit
  after insert or update of active on public.judge_gates
  for each row execute function public.audit_judge_gate_change();

alter table public.judge_gates enable row level security;

create policy judge_gates_select on public.judge_gates
  for select to authenticated using (public.is_org_member(org_id));
create policy judge_gates_insert on public.judge_gates
  for insert to authenticated with check (public.is_org_member(org_id));
create policy judge_gates_update on public.judge_gates
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

grant select, insert on public.judge_gates to authenticated;
grant update (active) on public.judge_gates to authenticated;
grant select, insert on public.judge_gates to service_role;
grant update (active) on public.judge_gates to service_role;

create or replace function public.publish_judge_gate(
  p_org_id uuid,
  p_gate text,
  p_model text,
  p_policy jsonb,
  p_fail_mode text default 'closed',
  p_allow_sensitive boolean default false,
  p_enabled boolean default true,
  p_max_state_chars integer default 20000,
  p_note text default null
)
returns public.judge_gates
language plpgsql
set search_path = ''
as $$
declare
  v_next integer;
  result public.judge_gates;
begin
  perform pg_advisory_xact_lock(
    hashtextextended('judge_gate:' || p_org_id::text || ':' || p_gate, 0));

  select coalesce(max(version), 0) + 1 into v_next
    from public.judge_gates where org_id = p_org_id and gate = p_gate;

  update public.judge_gates set active = false
   where org_id = p_org_id and gate = p_gate and active;

  insert into public.judge_gates
    (org_id, gate, version, enabled, model, fail_mode, allow_sensitive, max_state_chars,
     policy, note, active)
  values
    (p_org_id, p_gate, v_next, p_enabled, p_model, p_fail_mode, p_allow_sensitive,
     p_max_state_chars, p_policy, p_note, true)
  returning * into result;
  return result;
end;
$$;

create or replace function public.activate_judge_gate(
  p_org_id uuid,
  p_gate text,
  p_version integer
)
returns public.judge_gates
language plpgsql
set search_path = ''
as $$
declare
  result public.judge_gates;
begin
  perform pg_advisory_xact_lock(
    hashtextextended('judge_gate:' || p_org_id::text || ':' || p_gate, 0));

  if not exists (
    select 1 from public.judge_gates
     where org_id = p_org_id and gate = p_gate and version = p_version
  ) then
    raise exception 'Gate "%" has no version %', p_gate, p_version
      using errcode = 'no_data_found';
  end if;

  update public.judge_gates set active = false
   where org_id = p_org_id and gate = p_gate and active and version <> p_version;

  update public.judge_gates set active = true
   where org_id = p_org_id and gate = p_gate and version = p_version
  returning * into result;
  return result;
end;
$$;

revoke execute on function
  public.publish_judge_gate(uuid, text, text, jsonb, text, boolean, boolean, integer, text)
  from public, anon;
revoke execute on function public.activate_judge_gate(uuid, text, integer) from public, anon;
grant execute on function
  public.publish_judge_gate(uuid, text, text, jsonb, text, boolean, boolean, integer, text)
  to authenticated, service_role;
grant execute on function public.activate_judge_gate(uuid, text, integer)
  to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- Judgments: what each raw answer needs to be reproducible and costed
-- ---------------------------------------------------------------------------

-- Every question answered in one TypeSafe request shares a request_id, so the
-- request's tokens and latency (repeated on each row) are counted once by
-- grouping on it. The decision a gate reached is policy, not raw output, so it
-- is written to the judgment_made event rather than here.
alter table public.judgments
  add column request_id uuid,
  add column gate_version integer,
  add column agent_id uuid,
  add column model text,
  add column tokens_in integer check (tokens_in >= 0),
  add column tokens_out integer check (tokens_out >= 0),
  add column latency_ms integer check (latency_ms >= 0),
  add constraint judgments_agent_id_org_id_fkey
    foreign key (agent_id, org_id) references public.agents (id, org_id)
    on delete set null (agent_id);

comment on column public.judgments.request_id is
  'Shared by every question answered in one TypeSafe request.';
comment on column public.judgments.model is
  'The versioned model that answered, as TypeSafe reported it.';

create index judgments_request_idx on public.judgments (request_id);
