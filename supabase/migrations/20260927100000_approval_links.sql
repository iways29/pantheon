-- Approval links in the brief email (owner, 2026-09-26).
--
-- The morning brief ends with one link per approval waiting for the owner.
-- The link opens a page with the card and Approve / Reject buttons; nothing
-- is decided by opening it (mail scanners open links), only by pressing a
-- button, which posts the form.
--
-- - A list opts in with `approval_links_url`, the API's public address; null
--   means no links. `approval_link_hours` is how long a link works.
-- - An email carries links only when its writer asked (`emails.approval_links`:
--   the brief does, the evening question does not).
-- - Tokens are minted when the email is sent, by the backend, so an agent
--   never sees one: the stored email body has none. Only a SHA-256 hash is
--   kept. A link works once, until it expires, and only while its approval is
--   pending. Using one decides as `user_id` (the org's owner), through the
--   same `decide_approval` as every other decision.

alter table public.mailing_lists
  add column approval_links_url text
    check (approval_links_url ~ '^https://[^\s<>"'']+$'),
  add column approval_link_hours integer not null default 48
    check (approval_link_hours between 1 and 168);

comment on column public.mailing_lists.approval_links_url is
  'The API''s public address. Set: emails that ask for it end with one-tap '
  'approval links. Null: no links.';

alter table public.emails
  add column approval_links boolean not null default false;

-- The audit event names the new settings too.
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
                             'approval_links_url', subject.approval_links_url,
                             'approval_link_hours', subject.approval_link_hours,
                             'changed_by', auth.uid(), 'db_role', current_user));
  return subject;
end;
$$;

create table public.approval_links (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  approval_id uuid not null references public.approvals (id) on delete cascade,
  email_id uuid references public.emails (id) on delete set null,
  -- Who a decision through this link is made as.
  user_id uuid not null references auth.users (id) on delete cascade,
  token_hash text not null unique check (token_hash ~ '^[0-9a-f]{64}$'),
  expires_at timestamptz not null,
  used_at timestamptz,
  decision text check (decision in ('approve', 'cancel', 'redirect')),
  created_at timestamptz not null default now()
);

create index approval_links_approval_idx on public.approval_links (approval_id);
create index approval_links_email_idx on public.approval_links (email_id);

comment on table public.approval_links is
  'One-tap approval links in emails. Only hashes are stored; minted and used '
  'by the backend only.';

alter table public.approval_links enable row level security;
-- People may see that links exist (the Control Center, later); agents never.
-- Nobody but the backend writes them.
create policy approval_links_select on public.approval_links
  for select to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null);
grant select on public.approval_links to authenticated;
grant select, insert, update, delete on public.approval_links to service_role;
