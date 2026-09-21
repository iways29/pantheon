-- Step 1: organisations, membership, and the row-level security pattern that
-- every later table reuses.
--
-- Phase 1 has exactly one organisation, but the isolation is built now. It is
-- cheap to add here and expensive to retrofit once agents are writing rows.

create extension if not exists vector;
create extension if not exists pgcrypto;

-- Every table carries created_at/updated_at; this keeps updated_at honest
-- without each caller remembering to set it.
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create table public.orgs (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- orgs is the one table where the tenant key is the primary key rather than a
-- separate org_id column. The isolation rule is the same.
comment on table public.orgs is
  'Tenants. Phase 1 has one row; RLS is enforced from the start regardless.';

create trigger orgs_set_updated_at
  before update on public.orgs
  for each row execute function public.set_updated_at();

create table public.org_members (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  role text not null default 'owner' check (role in ('owner', 'admin', 'member')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, user_id)
);

create index org_members_user_id_idx on public.org_members (user_id);

create trigger org_members_set_updated_at
  before update on public.org_members
  for each row execute function public.set_updated_at();

-- Membership lookup used by every policy in the schema.
--
-- SECURITY DEFINER is load-bearing: the function owner is not subject to RLS,
-- so org_members policies can call it without recursing into themselves. The
-- search_path is pinned so the body cannot be hijacked by a caller's path.
create or replace function public.is_org_member(target_org_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
    from public.org_members m
    where m.org_id = target_org_id
      and m.user_id = auth.uid()
  );
$$;

alter table public.orgs enable row level security;
alter table public.org_members enable row level security;

create policy orgs_select_own on public.orgs
  for select to authenticated
  using (public.is_org_member(id));

create policy orgs_update_own on public.orgs
  for update to authenticated
  using (public.is_org_member(id))
  with check (public.is_org_member(id));

create policy org_members_select_own on public.org_members
  for select to authenticated
  using (public.is_org_member(org_id));

grant usage on schema public to authenticated, service_role;
grant select, update on public.orgs to authenticated;
grant select on public.org_members to authenticated;
grant execute on function public.is_org_member(uuid) to authenticated;

-- service_role is the backend identity for work with no authenticated caller
-- behind it. BYPASSRLS lets it past the policies but grants no privileges of
-- its own, so they are given explicitly here.
grant select, insert, update, delete on public.orgs to service_role;
grant select, insert, update, delete on public.org_members to service_role;
