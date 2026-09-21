-- Local-only scaffolding. NEVER applied to a Supabase project, which provides
-- all of this already.
--
-- Migrations in supabase/migrations/ are written against Supabase: they assume
-- the `auth` schema, `auth.uid()`, and the anon/authenticated/service_role
-- roles exist. This file recreates just enough of that on a plain PostgreSQL
-- server so the same migrations, and the same RLS policies, can be exercised
-- locally without Docker.

create schema if not exists auth;

do $$
begin
  create role anon nologin noinherit;
exception when duplicate_object then null;
end $$;

do $$
begin
  create role authenticated nologin noinherit;
exception when duplicate_object then null;
end $$;

do $$
begin
  create role service_role nologin noinherit bypassrls;
exception when duplicate_object then null;
end $$;

-- A minimal stand-in for Supabase's auth.users, enough to satisfy the foreign
-- key from org_members. Supabase's real table has far more columns.
create table if not exists auth.users (
  id uuid primary key default gen_random_uuid(),
  email text unique
);

-- Supabase exposes the verified JWT to SQL through the request.jwt.claims
-- setting. RLS policies read the caller's identity through auth.uid().
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  -- The inner nullif matters: an unauthenticated caller leaves the setting
  -- empty, and casting '' to jsonb raises instead of yielding null. Supabase's
  -- own auth.uid() returns null there, so this must too.
  select nullif(
    nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub',
    ''
  )::uuid
$$;

create or replace function auth.jwt()
returns jsonb
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claims', true), '')::jsonb,
    '{}'::jsonb
  )
$$;

grant usage on schema auth to anon, authenticated, service_role;
grant select on auth.users to authenticated;
grant select, insert, update, delete on auth.users to service_role;

-- A login role for local tests. Deliberately NOT a superuser: superusers
-- bypass RLS silently, which would make an isolation test pass without
-- proving anything. Tests connect as this role and SET ROLE to authenticated
-- or service_role, mirroring how Supabase switches roles per request.
do $$
begin
  create role pantheon login password 'pantheon' nosuperuser;
exception when duplicate_object then null;
end $$;

grant authenticated, service_role to pantheon;
grant usage on schema auth to pantheon;
