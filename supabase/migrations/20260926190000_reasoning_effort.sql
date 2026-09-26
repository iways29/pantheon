-- How hard each tier's model "thinks" before answering (owner, 2026-09-26).
--
-- Reasoning models spend output tokens thinking, and on OpenRouter those
-- count against a call's max_tokens. On 2026-09-26 the cheap tier (default
-- effort "high") spent every turn's budget thinking, so its tool calls were
-- cut off and a research run looped. The effort per tier is data: the
-- `reasoning` flag, {"<tier>": {"effort": "low"}}, sent by the gateway as
-- OpenRouter's `reasoning` parameter. A tier without an entry sends nothing
-- and gets the model's default. Changes are events.

insert into public.system_flags (org_id, key, value)
select id, 'reasoning', '{"cheap": {"effort": "low"}, "standard": {"effort": "low"}}'::jsonb
  from public.orgs
on conflict (org_id, key) do nothing;

create or replace function public.audit_reasoning_flag()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.key <> 'reasoning'
     or (tg_op = 'UPDATE' and new.value is not distinct from old.value) then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'reasoning_changed',
          jsonb_build_object('value', new.value, 'changed_by', auth.uid(),
                             'db_role', current_user));
  return new;
end;
$$;

revoke execute on function public.audit_reasoning_flag() from public, anon, authenticated;

create trigger system_flags_audit_reasoning
  after insert or update on public.system_flags
  for each row execute function public.audit_reasoning_flag();
