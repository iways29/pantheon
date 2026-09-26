-- Step 8.3: Marketing and Content drafts (ADR 029).
--
-- Three pieces:
--   facts.visibility  (exists) only a person clears a fact for public use,
--                     and public content may rely only on public facts.
--   drafts            one row per piece of content an agent writes. Agents
--                     write and check drafts; only a person approves, rejects
--                     or marks one as posted. Phase 1: the owner posts by hand.
--   artifact_claims   which facts a draft relied on or was checked against,
--                     so a wrong fact can be traced to every piece that used it
--                     (the owner's `unreal-lab-os` "blast radius" idea).
--
-- A draft ready for the owner waits on an approval card (action_type
-- draft_review), so it shows on the decision desk and in the morning brief.
-- The card and the draft move together: approved (with the owner's edits, if
-- any, kept as a voice example) or rejected.

-- --- Fact visibility -------------------------------------------------------------
--
-- facts.visibility exists since the write gate (20260925060000): every fact
-- starts internal and clearing one for public use is a deliberate step. Here:
-- only a person takes that step, and it is an event.

create or replace function public.fact_visibility_changed()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.visibility is not distinct from old.visibility then
    return new;
  end if;
  if public.current_agent_id() is not null then
    raise exception 'Only a person may change what is public'
      using errcode = 'insufficient_privilege';
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'fact_visibility_changed',
          jsonb_build_object('fact_id', new.id, 'claim', new.claim, 'from', old.visibility,
                             'to', new.visibility, 'changed_by', auth.uid()));
  return new;
end;
$$;

revoke execute on function public.fact_visibility_changed() from public, anon, authenticated;

create trigger facts_visibility_changed
  before update of visibility on public.facts
  for each row execute function public.fact_visibility_changed();

-- --- Drafts ----------------------------------------------------------------------------

create table public.drafts (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  agent_id uuid,
  task_id uuid,
  run_id uuid references public.runs (id) on delete set null,
  -- The piece this one is a new version of (the editor sent it back).
  revises uuid,
  channel text not null
    check (channel in ('blog', 'x', 'reddit', 'instagram', 'newsletter', 'other')),
  -- e.g. post, thread, carousel, section, outline, idea.
  format text not null check (format ~ '^[a-z][a-z0-9_-]{1,30}$'),
  title text not null check (length(title) between 3 and 200),
  body text not null check (length(body) between 1 and 40000),
  -- The idea or brief it answers, for the owner's context.
  brief text check (length(brief) <= 4000),
  -- draft: written; blocked: a check failed, back to the writer; ready:
  -- passed, waiting on the owner's approval card; approved; rejected;
  -- published: the owner posted it.
  status text not null default 'draft'
    check (status in ('draft', 'blocked', 'ready', 'approved', 'rejected', 'published')),
  -- The last check's results: per check, outcome and reasons, and per
  -- sentence for the claim check.
  checks jsonb not null default '{}'::jsonb,
  approval_id uuid references public.approvals (id) on delete set null,
  -- What the owner approved, if they edited it: a labelled voice example.
  owner_body text,
  owner_note text,
  published_url text check (published_url ~ '^https?://'),
  published_at timestamptz,
  idempotency_key text not null,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (id, org_id),
  unique (org_id, idempotency_key),
  foreign key (agent_id, org_id) references public.agents (id, org_id) on delete set null (agent_id),
  foreign key (task_id, org_id) references public.tasks (id, org_id) on delete set null (task_id),
  foreign key (revises, org_id) references public.drafts (id, org_id) on delete set null (revises)
);

comment on table public.drafts is
  'Content an agent wrote. Agents write and check; only a person approves, rejects or '
  'marks as posted. Owner edits kept in owner_body are voice examples.';

create index drafts_org_status on public.drafts (org_id, status, created_at desc);

create trigger drafts_set_updated_at
  before update on public.drafts
  for each row execute function public.set_updated_at();

-- What an agent may do to a draft, and what only a person may.
create or replace function public.draft_guard()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if public.is_backend() then
    return new;
  end if;
  if tg_op = 'INSERT' then
    if new.status <> 'draft' or new.owner_body is not null or new.published_at is not null then
      raise exception 'A new draft starts as a draft' using errcode = 'check_violation';
    end if;
    return new;
  end if;
  if public.current_agent_id() is not null then
    if old.status not in ('draft', 'blocked') then
      raise exception 'Draft % is with the owner; an agent may not change it', old.id
        using errcode = 'insufficient_privilege';
    end if;
    if new.status not in ('draft', 'blocked', 'ready')
       or new.owner_body is distinct from old.owner_body
       or new.owner_note is distinct from old.owner_note
       or new.published_url is distinct from old.published_url
       or new.published_at is distinct from old.published_at then
      raise exception 'Only a person approves, rejects or posts a draft'
        using errcode = 'insufficient_privilege';
    end if;
  end if;
  -- Ready means an approval card is waiting for the owner.
  if new.status = 'ready' and old.status <> 'ready' and not exists (
       select 1 from public.approvals a
        where a.id = new.approval_id and a.org_id = new.org_id
          and a.action_type = 'draft_review' and a.status = 'pending') then
    raise exception 'A ready draft needs a pending draft_review approval'
      using errcode = 'check_violation';
  end if;
  if new.status = 'published' and old.status not in ('approved', 'published') then
    raise exception 'Only an approved draft can be marked as posted'
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

revoke execute on function public.draft_guard() from public, anon, authenticated;

create trigger drafts_guard
  before insert or update on public.drafts
  for each row execute function public.draft_guard();

create or replace function public.audit_draft()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE' and new.status is not distinct from old.status then
    return new;
  end if;
  insert into public.events (org_id, run_id, agent_id, type, payload)
  values (new.org_id, new.run_id, new.agent_id, 'draft_' || new.status,
          jsonb_build_object('draft_id', new.id, 'channel', new.channel,
                             'format', new.format, 'title', new.title,
                             'approval_id', new.approval_id,
                             'edited', new.owner_body is not null,
                             'url', new.published_url));
  return new;
end;
$$;

revoke execute on function public.audit_draft() from public, anon, authenticated;

create trigger drafts_audit
  after insert or update on public.drafts
  for each row execute function public.audit_draft();

alter table public.drafts enable row level security;
create policy drafts_select on public.drafts
  for select to authenticated using (public.is_org_member(org_id));
create policy drafts_insert on public.drafts
  for insert to authenticated with check (public.is_org_member(org_id));
create policy drafts_update on public.drafts
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));
grant select, insert, update on public.drafts to authenticated;
grant select, insert, update, delete on public.drafts to service_role;

-- The card and the draft move together.
create or replace function public.approval_moves_draft()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.action_type <> 'draft_review' or new.status = old.status or old.status <> 'pending' then
    return new;
  end if;
  update public.drafts d
     set status = case when new.status = 'approved' then 'approved' else 'rejected' end,
         owner_body = case
           when new.status = 'approved'
                and new.edited_payload ? 'body'
                and new.edited_payload ->> 'body' is distinct from d.body
             then new.edited_payload ->> 'body' end,
         owner_note = case when new.verdict in ('approve', 'cancel', 'redirect') then null
                           else new.verdict end
   where d.approval_id = new.id and d.status = 'ready';
  return new;
end;
$$;

revoke execute on function public.approval_moves_draft() from public, anon, authenticated;

create trigger approvals_move_draft
  after update of status on public.approvals
  for each row execute function public.approval_moves_draft();

-- --- Which facts a draft used -----------------------------------------------------

create table public.artifact_claims (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  draft_id uuid not null,
  fact_id uuid not null,
  -- relied_on: the writer used it; checked: the claim check matched a
  -- sentence to it.
  relation text not null check (relation in ('relied_on', 'checked')),
  sentence text check (length(sentence) <= 2000),
  verdict text,
  created_at timestamptz not null default clock_timestamp(),
  foreign key (draft_id, org_id) references public.drafts (id, org_id) on delete cascade,
  foreign key (fact_id, org_id) references public.facts (id, org_id) on delete cascade
);

comment on table public.artifact_claims is
  'Which facts each draft relied on or was checked against. If a fact is wrong, '
  'every piece that used it can be found.';

create unique index artifact_claims_relied_once
  on public.artifact_claims (draft_id, fact_id) where relation = 'relied_on';
create index artifact_claims_fact on public.artifact_claims (org_id, fact_id);

-- Public content may rely only on public facts.
create or replace function public.artifact_claim_is_public()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.relation = 'relied_on' and not exists (
       select 1 from public.facts f
        where f.id = new.fact_id and f.org_id = new.org_id and f.visibility = 'public') then
    raise exception 'Fact % is internal; public content may rely only on public facts',
      new.fact_id using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

revoke execute on function public.artifact_claim_is_public() from public, anon, authenticated;

create trigger artifact_claims_public
  before insert on public.artifact_claims
  for each row execute function public.artifact_claim_is_public();

alter table public.artifact_claims enable row level security;
create policy artifact_claims_select on public.artifact_claims
  for select to authenticated using (public.is_org_member(org_id));
create policy artifact_claims_insert on public.artifact_claims
  for insert to authenticated with check (public.is_org_member(org_id));
grant select, insert on public.artifact_claims to authenticated;
grant select, insert, delete on public.artifact_claims to service_role;
