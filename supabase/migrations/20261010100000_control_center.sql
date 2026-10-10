-- The Control Center (Step 11, ADR 039): what its screens need that the
-- database did not have yet.
--
-- 1. Department changes are audited. A department's budget and on/off switch
--    change from the Control Center (through its charter), and every change
--    must reach `events` like every other setting.
-- 2. A standing rule can be retired. Rules are facts in the brain (the owner's
--    words, source 'owner', ref 'policy:...'); a retired one stays on record
--    but is never recalled again (brain search reads active and disputed
--    facts only).

create or replace function public.audit_department_change()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE' and (new.name, new.daily_budget_usd, new.enabled)
     is not distinct from (old.name, old.daily_budget_usd, old.enabled) then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id,
          case when tg_op = 'INSERT' then 'department_created' else 'department_updated' end,
          jsonb_build_object('department', new.name,
                             'daily_budget_usd', new.daily_budget_usd,
                             'enabled', new.enabled,
                             'was', case when tg_op = 'UPDATE' then
                               jsonb_build_object('daily_budget_usd', old.daily_budget_usd,
                                                  'enabled', old.enabled) end,
                             'changed_by', auth.uid(), 'db_role', current_user));
  return new;
end;
$$;

revoke execute on function public.audit_department_change() from public, anon, authenticated;

create trigger departments_audit
  after insert or update on public.departments
  for each row execute function public.audit_department_change();

alter table public.facts drop constraint facts_status_check;
alter table public.facts add constraint facts_status_check
  check (status in ('active', 'superseded', 'disputed', 'retired'));

comment on column public.facts.status is
  'active, disputed, superseded (by superseded_by), or retired (a standing rule '
  'the owner took back: kept on record, never recalled).';
