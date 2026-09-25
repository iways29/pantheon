-- Agent prompts as data, versioned, instead of constants in code.
--
-- ADR 007. Until now the research agent's two prompts were string constants in
-- api/app/agents/research.py, so changing one meant a code change and a
-- redeploy. A prompt is configuration the owner tunes often; it belongs beside
-- the model tier mapping (ADR 003), taking effect on the next run.
--
-- An agent has one prompt per named slot (the research agent has `answer` and
-- `extract`). Every edit is a new version and versions are never changed or
-- deleted, so any past run's behaviour can be reconstructed exactly. Exactly
-- one version per slot is active. Rolling back is activating an older version.

create table public.agent_prompts (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  agent_id uuid not null,
  slot text not null check (slot ~ '^[a-z][a-z0-9_]*$'),
  version integer not null check (version > 0),
  body text not null check (btrim(body) <> ''),
  -- Why this version exists, for the history screen. Optional.
  note text,
  active boolean not null default false,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now(),
  unique (agent_id, slot, version),
  foreign key (agent_id, org_id)
    references public.agents (id, org_id) on delete cascade
);

comment on table public.agent_prompts is
  'Versioned prompt text per agent and slot. Append-only apart from the active '
  'flag; each activation and deactivation is written to events.';

-- At most one active version per slot. A slot with none makes the agent's runs
-- pause (prompt_missing) rather than run on a guess.
create unique index agent_prompts_one_active
  on public.agent_prompts (agent_id, slot) where active;

-- A published version is history. Only the active flag may change, so nothing
-- can quietly rewrite a prompt a past run used -- including a hand edit in the
-- SQL editor.
create or replace function public.agent_prompts_freeze_history()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if (new.id, new.org_id, new.agent_id, new.slot, new.version, new.body, new.note,
      new.created_by, new.created_at)
     is distinct from
     (old.id, old.org_id, old.agent_id, old.slot, old.version, old.body, old.note,
      old.created_by, old.created_at)
  then
    raise exception 'A published prompt version cannot be edited; publish a new version'
      using errcode = 'restrict_violation';
  end if;
  return new;
end;
$$;

create trigger agent_prompts_freeze
  before update on public.agent_prompts
  for each row execute function public.agent_prompts_freeze_history();

-- Every change of which version is live is an operational event: it changes
-- what an agent does. A trigger, not application code, so nothing escapes the
-- audit trail. A switch from one version to another is a deactivated event
-- followed by an activated one.
create or replace function public.audit_agent_prompt_change()
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
    event_type := 'agent_prompt_activated';
  elsif new.active and not old.active then
    event_type := 'agent_prompt_activated';
  elsif old.active and not new.active then
    event_type := 'agent_prompt_deactivated';
  else
    return new;
  end if;

  insert into public.events (org_id, agent_id, type, payload)
  values (
    new.org_id,
    new.agent_id,
    event_type,
    jsonb_build_object(
      'slot', new.slot,
      'version', new.version,
      'changed_by', auth.uid(),
      'db_role', current_user
    )
  );
  return new;
end;
$$;

create trigger agent_prompts_audit
  after insert or update of active on public.agent_prompts
  for each row execute function public.audit_agent_prompt_change();

alter table public.agent_prompts enable row level security;

create policy agent_prompts_select on public.agent_prompts
  for select to authenticated using (public.is_org_member(org_id));
create policy agent_prompts_insert on public.agent_prompts
  for insert to authenticated with check (public.is_org_member(org_id));
create policy agent_prompts_update on public.agent_prompts
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));
-- No delete policy or grant: versions are never removed (an agent or org
-- deletion still cascades).

grant select, insert on public.agent_prompts to authenticated;
grant update (active) on public.agent_prompts to authenticated;
grant select, insert on public.agent_prompts to service_role;
grant update (active) on public.agent_prompts to service_role;

-- Publish a new version and make it the live one, in one step.
--
-- SECURITY INVOKER: it runs with the caller's rights, so RLS decides whether
-- the caller may touch this agent at all. The advisory lock serialises two
-- publishes to the same slot so their version numbers cannot collide.
create or replace function public.publish_agent_prompt(
  p_agent_id uuid,
  p_slot text,
  p_body text,
  p_note text default null
)
returns public.agent_prompts
language plpgsql
set search_path = ''
as $$
declare
  v_org_id uuid;
  v_next integer;
  result public.agent_prompts;
begin
  select org_id into v_org_id from public.agents where id = p_agent_id;
  if v_org_id is null then
    raise exception 'Agent % not found', p_agent_id using errcode = 'no_data_found';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_agent_id::text || ':' || p_slot, 0));

  select coalesce(max(version), 0) + 1 into v_next
    from public.agent_prompts where agent_id = p_agent_id and slot = p_slot;

  update public.agent_prompts set active = false
   where agent_id = p_agent_id and slot = p_slot and active;

  insert into public.agent_prompts (org_id, agent_id, slot, version, body, note, active)
  values (v_org_id, p_agent_id, p_slot, v_next, p_body, p_note, true)
  returning * into result;
  return result;
end;
$$;

-- Make an existing version the live one: the rollback, and the roll-forward.
create or replace function public.activate_agent_prompt(
  p_agent_id uuid,
  p_slot text,
  p_version integer
)
returns public.agent_prompts
language plpgsql
set search_path = ''
as $$
declare
  result public.agent_prompts;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_agent_id::text || ':' || p_slot, 0));

  if not exists (
    select 1 from public.agent_prompts
     where agent_id = p_agent_id and slot = p_slot and version = p_version
  ) then
    raise exception 'Agent % has no version % of prompt "%"', p_agent_id, p_version, p_slot
      using errcode = 'no_data_found';
  end if;

  update public.agent_prompts set active = false
   where agent_id = p_agent_id and slot = p_slot and active and version <> p_version;

  update public.agent_prompts set active = true
   where agent_id = p_agent_id and slot = p_slot and version = p_version
  returning * into result;
  return result;
end;
$$;

revoke execute on function public.publish_agent_prompt(uuid, text, text, text) from public, anon;
revoke execute on function public.activate_agent_prompt(uuid, text, integer) from public, anon;
grant execute on function public.publish_agent_prompt(uuid, text, text, text)
  to authenticated, service_role;
grant execute on function public.activate_agent_prompt(uuid, text, integer)
  to authenticated, service_role;

-- Which version of each prompt a run used, fixed when the run first executes.
-- Pinned so a run resumed after the owner edits a prompt still finishes on the
-- version it started with, and so the trace of any run names its prompts.
alter table public.runs
  add column prompt_versions jsonb not null default '{}'::jsonb;

comment on column public.runs.prompt_versions is
  'Slot to prompt version, e.g. {"answer": 2, "extract": 1}. Set on the run''s '
  'first invocation and never changed after.';

-- Bring existing research agents across: version 1 is the text they have been
-- running on. New agents get their starting prompts when they are created.
-- The text is a snapshot, so it is written out here rather than read from code.
insert into public.agent_prompts (org_id, agent_id, slot, version, body, note, active)
select a.org_id, a.id, p.slot, 1, p.body, 'Moved out of code (ADR 007)', true
  from public.agents a
 cross join (values
  ('answer',
   'You are a research assistant. Answer the question concisely, in at most five '
   'sentences. Use the known facts where they are relevant. If they do not cover the '
   'question, answer from general knowledge and say so.'),
  ('extract',
   'Extract standalone factual claims from the text. Each claim must make sense on its '
   'own, without the question or the other claims. Leave out opinions, hedges and '
   'anything the text says it is unsure of. Reply with JSON only, of the form '
   '{"claims": ["..."]}, with at most 5 claims. Reply with an empty list if there are '
   'none.')
 ) as p (slot, body)
 where a.role = 'research'
   and not exists (
     select 1 from public.agent_prompts x where x.agent_id = a.id and x.slot = p.slot
   );
