-- Step 6: documents, scoped to the company, a department or an agent (ADR 015).
--
-- Source material the owner uploads or links, kept apart from `facts`: a
-- document is what someone wrote; a fact is a checked claim. Every document
-- has a scope (owner decision 7, 2026-09-26):
--
--   company     every agent in the org may read it
--   department  only agents in that department
--   agent       only that one agent
--
-- The scope is enforced by RLS, not by application code. An agent's database
-- session names the agent (setting `pantheon.agent_id`, set by the API when it
-- acts for an agent); the owner's own session names none and sees everything
-- in the org. Naming an agent only ever narrows what is visible.

create or replace function public.current_agent_id()
returns uuid
language sql
stable
set search_path = ''
as $$
  select nullif(current_setting('pantheon.agent_id', true), '')::uuid;
$$;

comment on function public.current_agent_id() is
  'The agent this session acts for, or null for a person. Narrows document '
  'visibility to that agent''s scopes; never widens it.';

-- May the current session read something with this scope?
create or replace function public.can_read_scope(
  p_org_id uuid,
  p_scope text,
  p_department_id uuid,
  p_agent_id uuid
)
returns boolean
language sql
stable
set search_path = ''
as $$
  select public.is_org_member(p_org_id) and (
    public.current_agent_id() is null
    or p_scope = 'company'
    or (p_scope = 'agent' and p_agent_id = public.current_agent_id())
    or (p_scope = 'department' and p_department_id = (
          select a.department_id from public.agents a
           where a.id = public.current_agent_id() and a.org_id = p_org_id))
  );
$$;

revoke execute on function public.current_agent_id() from anon;
revoke execute on function public.can_read_scope(uuid, text, uuid, uuid) from anon;

create table public.documents (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  scope text not null check (scope in ('company', 'department', 'agent')),
  department_id uuid,
  agent_id uuid,
  title text not null check (btrim(title) <> ''),
  source_kind text not null check (source_kind in ('upload', 'link')),
  -- The file name, or the URL for a link.
  source_ref text not null,
  content_type text not null,
  bytes integer not null check (bytes >= 0),
  -- Same content in the same scope is the same document: re-uploading is safe.
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  -- Where the original file is in Supabase Storage (bucket `documents`).
  storage_path text,
  -- The screening label (Step 5.3). Only a clean document is chunked.
  status text not null check (status in ('clean', 'review', 'quarantined')),
  screening jsonb not null default '{}'::jsonb,
  chunks integer not null default 0 check (chunks >= 0),
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, org_id),
  foreign key (department_id, org_id)
    references public.departments (id, org_id) on delete cascade,
  foreign key (agent_id, org_id)
    references public.agents (id, org_id) on delete cascade,
  constraint documents_scope_owner check (
    (scope = 'company' and department_id is null and agent_id is null)
    or (scope = 'department' and department_id is not null and agent_id is null)
    or (scope = 'agent' and agent_id is not null and department_id is null)
  )
);

alter table public.documents
  add constraint documents_same_content_key
  unique nulls not distinct (org_id, scope, department_id, agent_id, sha256);

comment on table public.documents is
  'Uploaded or linked source material, scoped to company, department or agent. '
  'Kept apart from facts; screened before it is chunked.';

create trigger documents_set_updated_at
  before update on public.documents
  for each row execute function public.set_updated_at();

create table public.document_chunks (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  document_id uuid not null,
  -- Copied from the document so the read policy needs no join.
  scope text not null check (scope in ('company', 'department', 'agent')),
  department_id uuid,
  agent_id uuid,
  position integer not null check (position >= 0),
  text text not null check (btrim(text) <> ''),
  embedding vector(1536) not null,
  embedding_model text not null,
  created_at timestamptz not null default now(),
  unique (document_id, position),
  foreign key (document_id, org_id)
    references public.documents (id, org_id) on delete cascade
);

create index document_chunks_embedding_idx on public.document_chunks
  using hnsw (embedding vector_cosine_ops);
create index document_chunks_org_model_idx on public.document_chunks (org_id, embedding_model);

-- A fact taken from a document points back at it.
alter table public.facts
  add column document_id uuid,
  add constraint facts_document_id_org_id_fkey
    foreign key (document_id, org_id)
    references public.documents (id, org_id) on delete set null (document_id);

-- Every new document is an event, whatever made it.
create or replace function public.audit_document_added()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  insert into public.events (org_id, agent_id, type, payload)
  values (
    new.org_id,
    new.agent_id,
    'document_added',
    jsonb_build_object(
      'document_id', new.id,
      'title', new.title,
      'scope', new.scope,
      'department_id', new.department_id,
      'source_kind', new.source_kind,
      'source_ref', new.source_ref,
      'status', new.status,
      'changed_by', auth.uid(),
      'db_role', current_user
    )
  );
  return new;
end;
$$;

create trigger documents_audit
  after insert on public.documents
  for each row execute function public.audit_document_added();

alter table public.documents enable row level security;
alter table public.document_chunks enable row level security;

create policy documents_select on public.documents
  for select to authenticated
  using (public.can_read_scope(org_id, scope, department_id, agent_id));
-- Only a person adds documents; an agent session cannot.
create policy documents_insert on public.documents
  for insert to authenticated
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
create policy documents_update on public.documents
  for update to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null)
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);

create policy document_chunks_select on public.document_chunks
  for select to authenticated
  using (public.can_read_scope(org_id, scope, department_id, agent_id));
create policy document_chunks_insert on public.document_chunks
  for insert to authenticated
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);

grant select, insert, update on public.documents to authenticated;
grant select, insert on public.document_chunks to authenticated;
grant select, insert, update, delete on public.documents to service_role;
grant select, insert, update, delete on public.document_chunks to service_role;

-- The private bucket for original files. Guarded: the storage schema exists on
-- Supabase, not on the plain Postgres used for local tests and CI.
do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'storage') then
    insert into storage.buckets (id, name, public)
    values ('documents', 'documents', false)
    on conflict (id) do nothing;
  else
    raise notice 'No storage schema here; the documents bucket is not created.';
  end if;
end;
$$;
