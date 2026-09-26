-- Step 8.2: the morning brief by email (ADR 028).
--
-- Two tables:
--   mailing_lists  who gets an email, from which address, and whether it may
--                  go out without the owner's approval. Owner configuration:
--                  only a person may change it, and every change is an event.
--   emails         every email Pantheon sends, one row each, keyed so a
--                  serverless retry never sends twice. The send itself is
--                  done by the backend (Resend), which alone moves a row to
--                  sent or failed.
--
-- An email that needs approval waits on an approval card (action_type
-- send_email). The card and the email move together, here: approved lets the
-- backend send it; rejected or expired (a cancel, or the kill) cancels it.

-- --- Mailing lists -------------------------------------------------------------------

create table public.mailing_lists (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  -- What routines refer to, e.g. morning-brief.
  key text not null check (key ~ '^[a-z][a-z0-9-]{1,40}$'),
  name text not null,
  -- e.g. 'The Unreal Lab <newsletter@example.com>'; the domain must be
  -- verified in Resend.
  from_address text not null check (from_address ~ '^[^\r\n]*[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+>?$'),
  reply_to text check (reply_to ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  -- Resend takes at most 50 recipients per email.
  recipients text[] not null default '{}'
    check (cardinality(recipients) <= 50
           and array_to_string(recipients, ' ') !~ '[\r\n]'),
  -- {date} is replaced with the day the email is written, in `timezone`.
  subject text not null default 'Morning brief, {date}' check (length(subject) <= 200),
  timezone text not null default 'America/New_York',
  -- Off: every email waits for the owner's approval. On: the owner has
  -- decided this list's emails go out on their own.
  send_without_approval boolean not null default false,
  enabled boolean not null default true,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, key)
);

comment on table public.mailing_lists is
  'Who gets which email (owner configuration). Only people change it; every '
  'change is an event.';

create or replace function public.mailing_list_check_recipients()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  r text;
begin
  perform now() at time zone new.timezone;  -- an unknown time zone raises
  foreach r in array new.recipients loop
    if r !~ '^[^@\s<>,]+@[^@\s<>,]+\.[^@\s<>,]+$' then
      raise exception 'Not an email address: %', r using errcode = 'check_violation';
    end if;
  end loop;
  return new;
end;
$$;

create trigger mailing_lists_check_recipients
  before insert or update on public.mailing_lists
  for each row execute function public.mailing_list_check_recipients();

create trigger mailing_lists_set_updated_at
  before update on public.mailing_lists
  for each row execute function public.set_updated_at();

create or replace function public.audit_mailing_list()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  subject public.mailing_lists;
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
  values (subject.org_id, 'mailing_list_' || lower(tg_op),
          jsonb_build_object('list', subject.key, 'from', subject.from_address,
                             'recipients', to_jsonb(subject.recipients),
                             'send_without_approval', subject.send_without_approval,
                             'enabled', subject.enabled,
                             'changed_by', auth.uid(), 'db_role', current_user));
  return subject;
end;
$$;

create trigger mailing_lists_audit
  after insert or update or delete on public.mailing_lists
  for each row execute function public.audit_mailing_list();

alter table public.mailing_lists enable row level security;
create policy mailing_lists_select on public.mailing_lists
  for select to authenticated using (public.is_org_member(org_id));
create policy mailing_lists_write on public.mailing_lists
  for all to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null)
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
grant select, insert, update, delete on public.mailing_lists to authenticated;
grant select, insert, update, delete on public.mailing_lists to service_role;

-- --- Emails ----------------------------------------------------------------------------

create table public.emails (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  list_key text not null,
  task_id uuid references public.tasks (id) on delete set null,
  run_id uuid references public.runs (id) on delete set null,
  agent_id uuid references public.agents (id) on delete set null,
  approval_id uuid references public.approvals (id) on delete set null,
  -- held: waits for approval; ready: may be sent; sending: claimed by one
  -- invocation; sent; failed (may be retried); cancelled.
  status text not null
    check (status in ('held', 'ready', 'sending', 'sent', 'failed', 'cancelled')),
  from_address text not null,
  recipients text[] not null,
  subject text not null,
  body_text text not null,
  body_html text not null,
  idempotency_key text not null,
  provider_id text,
  error text,
  attempts integer not null default 0,
  created_at timestamptz not null default clock_timestamp(),
  sent_at timestamptz,
  unique (org_id, idempotency_key)
);

comment on table public.emails is
  'Every email Pantheon sends. Only the backend moves a row to sending, sent or failed.';

create index emails_org_created on public.emails (org_id, created_at desc);

create or replace function public.audit_email()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE' and new.status is not distinct from old.status then
    return new;
  end if;
  insert into public.events (org_id, run_id, agent_id, type, payload)
  values (new.org_id, new.run_id, new.agent_id, 'email_' || new.status,
          jsonb_build_object('email_id', new.id, 'list', new.list_key,
                             'recipients', cardinality(new.recipients),
                             'subject', new.subject, 'error', new.error,
                             'approval_id', new.approval_id));
  return new;
end;
$$;

create trigger emails_audit
  after insert or update on public.emails
  for each row execute function public.audit_email();

-- Outside the backend, an email is only ever the list's email: its sender and
-- recipients are the list's, and it may be ready to send only when the owner
-- let this list's emails go out on their own; otherwise it waits on a pending
-- send_email approval. An agent cannot pick recipients or skip approval.
create or replace function public.email_follows_its_list()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  l public.mailing_lists;
begin
  if public.is_backend() then
    return new;
  end if;
  select * into l from public.mailing_lists
   where org_id = new.org_id and key = new.list_key and enabled;
  if not found then
    raise exception 'No enabled mailing list %', new.list_key using errcode = 'check_violation';
  end if;
  if new.from_address is distinct from l.from_address
     or new.recipients is distinct from l.recipients then
    raise exception 'An email goes from and to its list only' using errcode = 'check_violation';
  end if;
  if new.status = 'ready' and not l.send_without_approval then
    raise exception 'List % needs the owner''s approval for each email', l.key
      using errcode = 'insufficient_privilege';
  end if;
  if new.status = 'held' and not exists (
       select 1 from public.approvals a
        where a.id = new.approval_id and a.org_id = new.org_id
          and a.action_type = 'send_email' and a.status = 'pending') then
    raise exception 'A held email needs a pending send_email approval'
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

revoke execute on function public.email_follows_its_list() from public, anon, authenticated;

create trigger emails_follow_their_list
  before insert on public.emails
  for each row execute function public.email_follows_its_list();

alter table public.emails enable row level security;
create policy emails_select on public.emails
  for select to authenticated using (public.is_org_member(org_id));
-- Agents (the brief writer) and people may write an email; only as held or
-- ready. Sending is the backend's.
create policy emails_insert on public.emails
  for insert to authenticated
  with check (public.is_org_member(org_id) and status in ('held', 'ready'));
grant select, insert on public.emails to authenticated;
grant select, insert, update on public.emails to service_role;

-- The card and the email move together.
create or replace function public.approval_moves_email()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.action_type <> 'send_email' or new.status = old.status or old.status <> 'pending' then
    return new;
  end if;
  update public.emails
     set status = case when new.status = 'approved' then 'ready' else 'cancelled' end,
         error = case when new.status = 'approved' then null
                      else 'Not approved: ' || coalesce(new.verdict, new.status) end
   where approval_id = new.id and status = 'held';
  return new;
end;
$$;

revoke execute on function public.approval_moves_email() from public, anon, authenticated;

create trigger approvals_move_email
  after update of status on public.approvals
  for each row execute function public.approval_moves_email();

revoke execute on function public.mailing_list_check_recipients() from public, anon, authenticated;
revoke execute on function public.audit_mailing_list() from public, anon, authenticated;
revoke execute on function public.audit_email() from public, anon, authenticated;
