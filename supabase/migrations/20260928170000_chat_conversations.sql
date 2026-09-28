-- Conversations in the chat (ADR 037, owner 2026-09-27: "New chat").
--
-- Each message belongs to a conversation with one agent. The agent reads only
-- its conversation's recent messages; the owner's preferences and the brain
-- carry across. A new conversation starts when the owner asks, or after the
-- chat flag's `idle_hours` without a word. Existing messages become each
-- agent's first conversation. Additive.

create table public.chat_conversations (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  agent_id uuid not null,
  -- The start of the owner's first message, for the list of past chats.
  title text not null default '' check (length(title) <= 120),
  created_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default clock_timestamp(),
  last_at timestamptz not null default clock_timestamp(),
  unique (id, org_id),
  foreign key (agent_id, org_id) references public.agents (id, org_id) on delete cascade
);

create index chat_conversations_agent_idx
  on public.chat_conversations (org_id, agent_id, last_at desc);

alter table public.chat_conversations enable row level security;

create policy chat_conversations_select on public.chat_conversations
  for select to authenticated using (public.is_org_member(org_id));
create policy chat_conversations_insert on public.chat_conversations
  for insert to authenticated with check (public.is_org_member(org_id));
create policy chat_conversations_update on public.chat_conversations
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

grant select, insert, update on public.chat_conversations to authenticated;
grant select, insert, update, delete on public.chat_conversations to service_role;

-- A new conversation is an event, like every message.
create or replace function public.audit_chat_conversation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  insert into public.events (org_id, agent_id, type, payload)
  values (new.org_id, new.agent_id, 'chat_started', jsonb_build_object('conversation_id', new.id));
  return new;
end;
$$;

revoke execute on function public.audit_chat_conversation() from public, anon, authenticated;

create trigger chat_conversations_audit
  after insert on public.chat_conversations
  for each row execute function public.audit_chat_conversation();

alter table public.chat_messages add column conversation_id uuid;
alter table public.chat_messages
  add constraint chat_messages_conversation_fk
  foreign key (conversation_id, org_id)
  references public.chat_conversations (id, org_id) on delete cascade;
create index chat_messages_conversation_idx
  on public.chat_messages (conversation_id, created_at);

-- What exists becomes each agent's first conversation (no event: nothing new
-- was said).
alter table public.chat_conversations disable trigger chat_conversations_audit;
insert into public.chat_conversations (org_id, agent_id, title, created_at, last_at)
select m.org_id, m.agent_id,
       coalesce(left((select o.body from public.chat_messages o
                       where o.agent_id = m.agent_id and o.role = 'owner'
                       order by o.created_at limit 1), 120), ''),
       min(m.created_at), max(m.created_at)
  from public.chat_messages m
 group by m.org_id, m.agent_id;
alter table public.chat_conversations enable trigger chat_conversations_audit;

update public.chat_messages m
   set conversation_id = c.id
  from public.chat_conversations c
 where c.agent_id = m.agent_id and c.org_id = m.org_id and m.conversation_id is null;

-- A quiet gap after which the next message starts a new conversation.
update public.system_flags
   set value = value || '{"idle_hours": 12}'::jsonb
 where key = 'chat' and not value ? 'idle_hours';
