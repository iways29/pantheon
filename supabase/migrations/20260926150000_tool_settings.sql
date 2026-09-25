-- Tool settings as data (ADR 026).
--
-- Some tools have knobs the owner should turn without a deploy: web search's
-- engine, depth, number of results and allowed or excluded sites. They live on
-- the tool's row, audited like every other change to it.

alter table public.tools
  add column settings jsonb not null default '{}'::jsonb
    check (jsonb_typeof(settings) = 'object');

comment on column public.tools.settings is
  'Owner-editable knobs a tool reads at call time, e.g. web search engine and max results.';

-- The tool audit also records settings changes.
create or replace function public.audit_tool_change()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  subject public.tools;
begin
  if tg_op = 'DELETE' then
    if not exists (select 1 from public.orgs where id = old.org_id) then
      return old;
    end if;
    subject := old;
  else
    subject := new;
  end if;
  insert into public.events (org_id, type, payload)
  values (
    subject.org_id,
    case tg_op when 'INSERT' then 'tool_created' when 'UPDATE' then 'tool_updated'
               else 'tool_deleted' end,
    jsonb_build_object(
      'tool', subject.name,
      'risk_class', subject.risk_class,
      'approval', subject.approval,
      'enabled', subject.enabled,
      'settings', subject.settings,
      'max_calls_per_day', subject.max_calls_per_day,
      'changed_by', auth.uid(),
      'db_role', current_user
    )
  );
  return subject;
end;
$$;
