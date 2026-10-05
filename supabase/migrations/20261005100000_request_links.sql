-- Ask for tomorrow's topics from the email itself (owner, 2026-10-05).
--
-- The brief and the evening question end with a small form: what should a
-- morning routine (research, marketing) look at next? Submitting it adds a
-- `routine_requests` row, exactly as `scripts.department ask` does, used once
-- by the routine's next run.
--
-- - Which routines a list's form offers is data: `request_routines`. Empty
--   means no form.
-- - An email carries the form only when its writer asks (`emails.request_form`).
-- - The form posts to a link minted when the email is sent, by the backend,
--   so no agent ever sees a token; only its SHA-256 hash is kept. Unlike an
--   approval link it can be used a few times (a few topics), until it expires
--   (the list's `approval_link_hours`). Each request is made as the org's
--   owner, so it is audited like any other.

alter table public.mailing_lists
  add column request_routines text[] not null default '{}'
    check (cardinality(request_routines) <= 10);

comment on column public.mailing_lists.request_routines is
  'Routines (triggers.routine_key) the owner can ask topics of from the form at '
  'the end of this list''s emails. Empty: no form. Needs approval_links_url.';

alter table public.emails
  add column request_form boolean not null default false;

-- The audit event names the new setting too.
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
                             'request_routines', to_jsonb(subject.request_routines),
                             'changed_by', auth.uid(), 'db_role', current_user));
  return subject;
end;
$$;

create table public.request_links (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  email_id uuid references public.emails (id) on delete set null,
  -- Who a request through this link is made as.
  user_id uuid not null references auth.users (id) on delete cascade,
  token_hash text not null unique check (token_hash ~ '^[0-9a-f]{64}$'),
  -- The routines this form offers, fixed when it was sent.
  routines text[] not null check (cardinality(routines) between 1 and 10),
  expires_at timestamptz not null,
  uses integer not null default 0 check (uses >= 0),
  max_uses integer not null default 5 check (max_uses between 1 and 20),
  last_used_at timestamptz,
  created_at timestamptz not null default now()
);

create index request_links_email_idx on public.request_links (email_id);

comment on table public.request_links is
  'The topic form in emails. Only hashes are stored; minted and used by the '
  'backend only.';

alter table public.request_links enable row level security;
-- People may see that links exist; agents never. Nobody but the backend
-- writes them.
create policy request_links_select on public.request_links
  for select to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null);
grant select on public.request_links to authenticated;
grant select, insert, update, delete on public.request_links to service_role;
