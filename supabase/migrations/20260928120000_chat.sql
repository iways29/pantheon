-- The chat (Step 10, ADR 037): the owner talks with the Chief of Staff or any
-- department head, and gets an answer in seconds.
--
-- 1. `chat_messages`: one row per message, the owner's and the agent's, each
--    with the agent it was said to. A message that started work names its task.
-- 2. `start_now()`: what the minute tick does (queued tasks become runs, runs
--    are pushed to the API), done now, so an order from chat starts at once
--    instead of within a minute. Only a person in an org may call it; the
--    pause still holds (dispatch skips a paused org).
-- 3. The `chat` flag: reply length, history, facts recalled. Data, audited.

create table public.chat_messages (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  -- The agent the owner is talking with (both sides of the thread carry it).
  agent_id uuid not null,
  role text not null check (role in ('owner', 'agent')),
  body text not null check (length(body) between 1 and 8000),
  -- Work this message started, if any.
  task_id uuid,
  -- Model, tokens and cost of a reply; empty for the owner's messages.
  meta jsonb not null default '{}'::jsonb,
  -- The owner's messages carry the browser's key: a retried send is one message.
  idempotency_key text,
  -- When the owner's words were sorted into memory (ADR 034), or null.
  remembered_at timestamptz,
  created_by uuid references auth.users (id) on delete set null,
  -- The clock, not the transaction's start: a reply always sorts after its question.
  created_at timestamptz not null default clock_timestamp(),
  unique (id, org_id),
  unique (org_id, idempotency_key),
  foreign key (agent_id, org_id) references public.agents (id, org_id) on delete cascade,
  foreign key (task_id, org_id) references public.tasks (id, org_id) on delete set null (task_id)
);

create index chat_messages_thread_idx on public.chat_messages (org_id, agent_id, created_at desc);
create index chat_messages_unsorted_idx on public.chat_messages (org_id, created_at)
  where role = 'owner' and remembered_at is null;

alter table public.chat_messages enable row level security;

create policy chat_messages_select on public.chat_messages
  for select to authenticated using (public.is_org_member(org_id));
create policy chat_messages_insert on public.chat_messages
  for insert to authenticated with check (public.is_org_member(org_id));
create policy chat_messages_update on public.chat_messages
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

grant select, insert, update on public.chat_messages to authenticated;
grant select, insert, update, delete on public.chat_messages to service_role;

-- Every message is an event: the brain screen lights the agent up.
create or replace function public.audit_chat_message()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  insert into public.events (org_id, agent_id, type, payload)
  values (new.org_id, new.agent_id,
          case when new.role = 'owner' then 'chat_said' else 'chat_replied' end,
          jsonb_build_object('message_id', new.id, 'task_id', new.task_id));
  return new;
end;
$$;

revoke execute on function public.audit_chat_message() from public, anon, authenticated;

create trigger chat_messages_audit
  after insert on public.chat_messages
  for each row execute function public.audit_chat_message();

-- Start queued work now rather than at the next minute.
create or replace function public.start_now()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  started integer;
begin
  if public.current_agent_id() is not null
     or not (public.is_backend()
             or exists (select 1 from public.org_members where user_id = auth.uid())) then
    raise exception 'Only a person in an org may start work now'
      using errcode = 'insufficient_privilege';
  end if;
  select count(*) into started from public.dispatch_queued_tasks();
  -- Pushing needs the Vault secrets (Supabase); without them the tick waits.
  if to_regclass('vault.decrypted_secrets') is not null then
    perform public.push_runs();
  end if;
  return started;
end;
$$;

revoke execute on function public.start_now() from public, anon;
grant execute on function public.start_now() to authenticated, service_role;

-- How the chat behaves: data, audited.
insert into public.system_flags (org_id, key, value)
select id, 'chat',
       '{"max_tokens": 900, "history": 20, "facts": 5, "orders": 5}'::jsonb
  from public.orgs
on conflict (org_id, key) do nothing;

create or replace function public.audit_chat_flag()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.key <> 'chat'
     or (tg_op = 'UPDATE' and new.value is not distinct from old.value) then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'chat_settings_changed',
          jsonb_build_object('value', new.value, 'changed_by', auth.uid(),
                             'db_role', current_user));
  return new;
end;
$$;

revoke execute on function public.audit_chat_flag() from public, anon, authenticated;

create trigger system_flags_audit_chat
  after insert or update on public.system_flags
  for each row execute function public.audit_chat_flag();
