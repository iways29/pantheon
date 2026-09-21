-- Which model each tier resolves to, as data rather than deployment config.
--
-- ADR 003. Step 2 kept this mapping in the MODEL_TIERS environment variable,
-- so every model swap needed a redeploy. A row here takes effect on the very
-- next gateway call. MODEL_TIERS survives as the default for any tier an org
-- has not set.

create table public.model_tier_assignments (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  -- NULL is the org-wide mapping. A department row overrides it for that
  -- department only, so one team can trial a model without moving everyone.
  department_id uuid,
  tier text not null check (tier in ('cheap', 'standard', 'frontier')),
  -- Explicit versioned slugs only. OpenRouter's `~vendor/family-latest`
  -- aliases repoint themselves, which would change cost and behaviour with no
  -- signal here; see ADR 003. Whether the slug exists is checked by the
  -- application against OpenRouter's catalogue, which SQL cannot reach.
  model text not null
    check (model <> '' and position('~' in model) = 0 and model = btrim(model)),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (department_id, org_id)
    references public.departments (id, org_id) on delete cascade
);

-- One org-wide row per tier, and one per department per tier. NULLS NOT
-- DISTINCT makes the org-wide row (department_id NULL) unique too.
alter table public.model_tier_assignments
  add constraint model_tier_assignments_scope_key
  unique nulls not distinct (org_id, department_id, tier);

comment on table public.model_tier_assignments is
  'Tier to model mapping per org, optionally overridden per department. Read '
  'by the gateway on every call; every change is written to events.';

create trigger model_tier_assignments_set_updated_at
  before update on public.model_tier_assignments
  for each row execute function public.set_updated_at();

-- A model change alters cost and behaviour, so it is an operational event.
-- Done in a trigger rather than in application code so that nothing escapes
-- the audit trail -- including an edit made by hand in the SQL editor.
-- SECURITY INVOKER: the caller must be allowed to write events for this org,
-- which any member already is.
create or replace function public.audit_model_tier_change()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  subject public.model_tier_assignments;
begin
  if tg_op = 'DELETE' then
    subject := old;
  else
    subject := new;
  end if;

  if tg_op = 'UPDATE' and old.model = new.model then
    return new;
  end if;

  insert into public.events (org_id, type, payload)
  values (
    subject.org_id,
    'model_tier_changed',
    jsonb_build_object(
      'tier', subject.tier,
      'department_id', subject.department_id,
      'from', case when tg_op = 'INSERT' then null else old.model end,
      'to', case when tg_op = 'DELETE' then null else new.model end,
      'operation', lower(tg_op),
      'changed_by', auth.uid(),
      'db_role', current_user
    )
  );

  return subject;
end;
$$;

create trigger model_tier_assignments_audit
  after insert or update or delete on public.model_tier_assignments
  for each row execute function public.audit_model_tier_change();

alter table public.model_tier_assignments enable row level security;

create policy model_tier_assignments_select on public.model_tier_assignments
  for select to authenticated using (public.is_org_member(org_id));
create policy model_tier_assignments_insert on public.model_tier_assignments
  for insert to authenticated with check (public.is_org_member(org_id));
create policy model_tier_assignments_update on public.model_tier_assignments
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));
create policy model_tier_assignments_delete on public.model_tier_assignments
  for delete to authenticated using (public.is_org_member(org_id));

grant select, insert, update, delete on public.model_tier_assignments to authenticated;
grant select, insert, update, delete on public.model_tier_assignments to service_role;
