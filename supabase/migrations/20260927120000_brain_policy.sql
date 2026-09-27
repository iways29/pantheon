-- What may enter the brain (owner, 2026-09-27; ADR 033).
--
-- The brain grows with use: the company's own sources, what the owner says
-- to the agents and what the owner does. Not with whatever an agent read on
-- the web: news goes stale and can be found again with a search.
--
-- The `brain_policy` flag, data like every other rule:
--   agent_web_facts  false: pages an agent reads are findings for the day
--                    (the brief lists them), not facts for the brain.
--   agent_facts      false: an agent may not propose facts of its own.
--   remember_orders, remember_asks, remember_approved_drafts  true: each
--                    morning the brief writer turns the owner's orders,
--                    research questions and approved drafts since the last
--                    brief into facts from the owner.
-- The owner's own pushes (links, documents) and decisions with notes are
-- unaffected. Changes are events.

insert into public.system_flags (org_id, key, value)
select id, 'brain_policy',
       '{"agent_web_facts": false, "agent_facts": false, "remember_orders": true,
         "remember_asks": true, "remember_approved_drafts": true}'::jsonb
  from public.orgs
on conflict (org_id, key) do nothing;

create or replace function public.audit_brain_policy_flag()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.key <> 'brain_policy'
     or (tg_op = 'UPDATE' and new.value is not distinct from old.value) then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'brain_policy_changed',
          jsonb_build_object('value', new.value, 'changed_by', auth.uid(),
                             'db_role', current_user));
  return new;
end;
$$;

revoke execute on function public.audit_brain_policy_flag() from public, anon, authenticated;

create trigger system_flags_audit_brain_policy
  after insert or update on public.system_flags
  for each row execute function public.audit_brain_policy_flag();
