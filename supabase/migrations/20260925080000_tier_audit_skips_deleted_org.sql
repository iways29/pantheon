-- Deleting an org must not fail because of its model tier assignments.
--
-- audit_model_tier_change() (20260921030000) writes an event for every
-- insert, update and delete. When an org is deleted, the cascade removes its
-- tier assignments, the trigger fires, and the event insert fails on
-- events_org_id_fkey because the org is already gone, which aborts the whole
-- delete. The same guard as audit_trigger_change (20260925040000) and
-- audit_model_price_change (20260925050000): a delete whose org no longer
-- exists is a cascade, and there is no org left to audit against.
--
-- Those two already had the guard; this was the only audit trigger that fires
-- on delete without it. Behaviour is otherwise unchanged.

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
    -- Deleting the org cascades here; there is no org left to audit against.
    if not exists (select 1 from public.orgs where id = old.org_id) then
      return old;
    end if;
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
