-- Step 8.0: department charters as data (ADR 024).
--
-- A charter says what a department is for and how it works: its purpose,
-- head and workers (with their tools, tiers, runners and starting prompts),
-- its morning routine, approval rules, Jev gates, budget, autonomy level,
-- data sensitivity and what "working" means. The owner edits it without code;
-- `python -m scripts.department apply` (later the Control Center) makes the
-- department, agents and routine match it. Agents and routine triggers it
-- creates start switched off; the owner reviews and switches them on.
--
-- Versioned like prompts and gates: a published version is never edited, one
-- version is live, and every activation is an event. The shape of `charter`
-- is validated by the application (app/departments/charter.py).

create table public.department_charters (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  department text not null check (btrim(department) <> ''),
  version integer not null check (version > 0),
  charter jsonb not null check (jsonb_typeof(charter) = 'object'),
  note text,
  active boolean not null default false,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now(),
  unique (org_id, department, version)
);

comment on table public.department_charters is
  'Versioned department charters: purpose, agents, routine, rules, budget. '
  'Append-only apart from the active flag; each activation is an event.';

create unique index department_charters_one_active
  on public.department_charters (org_id, department) where active;

create or replace function public.department_charters_freeze()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if (new.id, new.org_id, new.department, new.version, new.charter, new.note, new.created_by,
      new.created_at)
     is distinct from
     (old.id, old.org_id, old.department, old.version, old.charter, old.note, old.created_by,
      old.created_at) then
    raise exception 'A published charter cannot be edited; publish a new version'
      using errcode = 'restrict_violation';
  end if;
  return new;
end;
$$;

create trigger department_charters_freeze
  before update on public.department_charters
  for each row execute function public.department_charters_freeze();

create or replace function public.audit_department_charter()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE' and new.active = old.active then
    return new;
  end if;
  if not new.active then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'charter_activated',
          jsonb_build_object('department', new.department, 'version', new.version,
                             'note', new.note, 'changed_by', auth.uid(),
                             'db_role', current_user));
  return new;
end;
$$;

create trigger department_charters_audit
  after insert or update of active on public.department_charters
  for each row execute function public.audit_department_charter();

alter table public.department_charters enable row level security;
create policy department_charters_select on public.department_charters
  for select to authenticated using (public.is_org_member(org_id));
-- Only a person writes charters; never an agent's session.
create policy department_charters_insert on public.department_charters
  for insert to authenticated
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
create policy department_charters_update on public.department_charters
  for update to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null)
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);

grant select, insert on public.department_charters to authenticated;
grant update (active) on public.department_charters to authenticated;
grant select, insert on public.department_charters to service_role;
grant update (active) on public.department_charters to service_role;

-- Publish a new version and make it live. SECURITY INVOKER: RLS decides.
create or replace function public.publish_department_charter(
  p_org_id uuid, p_department text, p_charter jsonb, p_note text default null
)
returns public.department_charters
language plpgsql
set search_path = ''
as $$
declare
  v_next integer;
  result public.department_charters;
begin
  perform pg_advisory_xact_lock(
    hashtextextended('charter:' || p_org_id::text || ':' || p_department, 0));
  select coalesce(max(version), 0) + 1 into v_next
    from public.department_charters where org_id = p_org_id and department = p_department;
  update public.department_charters set active = false
   where org_id = p_org_id and department = p_department and active;
  insert into public.department_charters (org_id, department, version, charter, note, active)
  values (p_org_id, p_department, v_next, p_charter, p_note, true)
  returning * into result;
  return result;
end;
$$;

revoke execute on function public.publish_department_charter(uuid, text, jsonb, text)
  from public, anon;
grant execute on function public.publish_department_charter(uuid, text, jsonb, text)
  to authenticated, service_role;

-- A routine trigger remembers which charter item made it, so applying a
-- charter again updates that trigger instead of adding another.
alter table public.triggers
  add column routine_key text;

create unique index triggers_routine_key
  on public.triggers (org_id, routine_key) where routine_key is not null;
