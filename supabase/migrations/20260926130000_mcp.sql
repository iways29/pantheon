-- Step 7.7: tools from MCP servers, added and approved by the owner (ADR 025).
--
-- The owner adds a remote MCP server (Streamable HTTP; Vercel cannot run a
-- local stdio server). Pantheon lists its tools and stores each as a row in
-- `tools`, like a built-in tool, so every rule already there applies: risk
-- classes, the autonomy ladder, the tool-risk gate, approvals, loop
-- detection, the pause and the kill, the audit trail.
--
-- Three rules are enforced here, not only in code:
--   1. A tool found on a server starts switched off, until the owner has
--      read it and approved it.
--   2. Approval is of an exact definition (name, description, input
--      schema). If the server changes it, the tool switches itself off until
--      the owner approves the new one: a server cannot change what an agent
--      is told a tool does behind the owner's back.
--   3. Credentials never reach a browser or an agent: they are stored in
--      Supabase Vault (encrypted), readable only by the backend.

-- --- Servers -------------------------------------------------------------------

create table public.mcp_servers (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  -- Short name; tools from it are named mcp_<name>_<tool>.
  name text not null check (name ~ '^[a-z][a-z0-9]{1,20}$'),
  url text not null check (url ~ '^https://'),
  auth text not null default 'oauth' check (auth in ('none', 'bearer', 'oauth')),
  -- new: added; needs_auth: sign-in needed; connected: tools can be listed
  -- and called; error: the last attempt failed (see last_error).
  status text not null default 'new'
    check (status in ('new', 'needs_auth', 'connected', 'error')),
  last_error text,
  last_listed_at timestamptz,
  enabled boolean not null default true,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (org_id, name),
  unique (id, org_id)
);

comment on table public.mcp_servers is
  'Remote MCP servers the owner added. Their tools are rows in tools '
  '(source mcp); their credentials are in Vault, never in this table.';

create trigger mcp_servers_set_updated_at
  before update on public.mcp_servers
  for each row execute function public.set_updated_at();

create or replace function public.audit_mcp_server()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  subject public.mcp_servers;
begin
  if tg_op = 'DELETE' then
    if not exists (select 1 from public.orgs where id = old.org_id) then
      return old;
    end if;
    subject := old;
  else
    subject := new;
  end if;
  if tg_op = 'UPDATE' and (new.url, new.auth, new.status, new.enabled)
                          is not distinct from (old.url, old.auth, old.status, old.enabled) then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (subject.org_id, 'mcp_server_' || lower(tg_op),
          jsonb_build_object('server', subject.name, 'url', subject.url, 'auth', subject.auth,
                             'status', subject.status, 'enabled', subject.enabled,
                             'changed_by', auth.uid(), 'db_role', current_user));
  return subject;
end;
$$;

create trigger mcp_servers_audit
  after insert or update or delete on public.mcp_servers
  for each row execute function public.audit_mcp_server();

alter table public.mcp_servers enable row level security;
create policy mcp_servers_select on public.mcp_servers
  for select to authenticated using (public.is_org_member(org_id));
create policy mcp_servers_write on public.mcp_servers
  for all to authenticated
  using (public.is_org_member(org_id) and public.current_agent_id() is null)
  with check (public.is_org_member(org_id) and public.current_agent_id() is null);
grant select, insert, update, delete on public.mcp_servers to authenticated;
grant select, insert, update, delete on public.mcp_servers to service_role;

-- --- Tools from servers --------------------------------------------------------------

alter table public.tools
  add column source text not null default 'builtin' check (source in ('builtin', 'mcp')),
  add column mcp_server_id uuid,
  -- The tool's own name on its server.
  add column remote_name text,
  add column input_schema jsonb,
  -- The server's own hints (readOnlyHint, destructiveHint, ...): advice only.
  add column annotations jsonb,
  -- What the risk class would be from those hints; the owner decides.
  add column suggested_risk text check (suggested_risk in ('R0', 'R1', 'R2', 'R3', 'R4')),
  -- sha256 of the definition as last listed, and as the owner approved it.
  add column definition_sha text,
  add column approved_sha text,
  add column approved_at timestamptz,
  add column approved_by uuid,
  -- Optional ceiling on successful calls per day (e.g. a paid generator).
  add column max_calls_per_day integer check (max_calls_per_day between 1 and 10000),
  add constraint tools_mcp_server_fkey foreign key (mcp_server_id, org_id)
    references public.mcp_servers (id, org_id) on delete cascade,
  add constraint tools_mcp_shape check (
    (source = 'builtin' and mcp_server_id is null)
    or (source = 'mcp' and mcp_server_id is not null and remote_name is not null
        and definition_sha is not null)),
  -- Rule 2: an MCP tool is on only while its definition is the approved one.
  add constraint tools_mcp_on_only_as_approved check (
    source <> 'mcp' or not enabled or approved_sha = definition_sha);

-- Rules 1 and 2 as behaviour: a newly listed tool starts off; a changed
-- definition switches it off and forgets nothing (the approved sha stays, so
-- the owner can see it differs).
create or replace function public.mcp_tool_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.source <> 'mcp' then
    return new;
  end if;
  if tg_op = 'INSERT' then
    new.enabled := false;
    new.approved_sha := null;
  elsif new.definition_sha is distinct from old.definition_sha
        and new.definition_sha is distinct from new.approved_sha then
    new.enabled := false;
    insert into public.events (org_id, type, payload)
    values (new.org_id, 'mcp_tool_changed',
            jsonb_build_object('tool', new.name, 'was_on', old.enabled));
  end if;
  return new;
end;
$$;

create trigger tools_mcp_guard
  before insert or update on public.tools
  for each row execute function public.mcp_tool_guard();

-- --- Credentials, in Vault ----------------------------------------------------------

-- Which Vault secret holds which credential. The secret itself is only ever
-- in vault.secrets (encrypted at rest); this row is a pointer. On a plain
-- Postgres without Vault (local tests) the value is kept in `local_value`
-- instead, which production never uses.
create table public.mcp_credentials (
  org_id uuid not null references public.orgs (id) on delete cascade,
  server_id uuid not null,
  -- bearer: a static token; oauth: tokens and client registration (JSON);
  -- oauth_pending: a sign-in in progress (PKCE verifier, state).
  kind text not null check (kind in ('bearer', 'oauth', 'oauth_pending')),
  vault_secret_id uuid,
  local_value text,
  -- For oauth_pending: the state value the callback arrives with.
  state text,
  expires_at timestamptz,
  updated_at timestamptz not null default now(),
  primary key (server_id, kind),
  foreign key (server_id, org_id) references public.mcp_servers (id, org_id) on delete cascade
);

create unique index mcp_credentials_state on public.mcp_credentials (state) where state is not null;

-- No policy for `authenticated`: with RLS on and no policy, no signed-in
-- user (or agent) can read a row. Only the backend (service_role) may.
alter table public.mcp_credentials enable row level security;
revoke all on public.mcp_credentials from anon, authenticated;
grant select, insert, update, delete on public.mcp_credentials to service_role;

create or replace function public.mcp_put_credential(
  p_org_id uuid, p_server_id uuid, p_kind text, p_value text,
  p_state text default null, p_expires_at timestamptz default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  existing uuid;
  has_vault boolean := exists (select 1 from pg_namespace where nspname = 'vault');
begin
  if not public.is_backend() then
    raise exception 'Only the backend stores credentials' using errcode = 'insufficient_privilege';
  end if;
  select vault_secret_id into existing from public.mcp_credentials
   where server_id = p_server_id and kind = p_kind;
  if has_vault then
    if existing is null then
      execute 'select vault.create_secret($1, $2)' into existing
        using p_value, 'mcp:' || p_server_id || ':' || p_kind || ':' || gen_random_uuid();
    else
      execute 'select vault.update_secret($1, $2)' using existing, p_value;
    end if;
  end if;
  insert into public.mcp_credentials
    (org_id, server_id, kind, vault_secret_id, local_value, state, expires_at, updated_at)
  values (p_org_id, p_server_id, p_kind, existing,
          case when has_vault then null else p_value end, p_state, p_expires_at, now())
  on conflict (server_id, kind) do update
     set vault_secret_id = excluded.vault_secret_id, local_value = excluded.local_value,
         state = excluded.state, expires_at = excluded.expires_at, updated_at = now();
end;
$$;

create or replace function public.mcp_get_credential(p_server_id uuid, p_kind text)
returns text
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  c public.mcp_credentials;
  value text;
begin
  if not public.is_backend() then
    raise exception 'Only the backend reads credentials' using errcode = 'insufficient_privilege';
  end if;
  select * into c from public.mcp_credentials where server_id = p_server_id and kind = p_kind;
  if not found then
    return null;
  end if;
  if c.vault_secret_id is not null then
    execute 'select decrypted_secret from vault.decrypted_secrets where id = $1'
      into value using c.vault_secret_id;
    return value;
  end if;
  return c.local_value;
end;
$$;

create or replace function public.mcp_drop_credential(p_server_id uuid, p_kind text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not public.is_backend() then
    raise exception 'Only the backend removes credentials' using errcode = 'insufficient_privilege';
  end if;
  -- The cleanup trigger removes the Vault secret.
  delete from public.mcp_credentials where server_id = p_server_id and kind = p_kind;
end;
$$;

revoke execute on function public.mcp_put_credential(uuid, uuid, text, text, text, timestamptz)
  from public, anon, authenticated;
revoke execute on function public.mcp_get_credential(uuid, text) from public, anon, authenticated;
revoke execute on function public.mcp_drop_credential(uuid, text) from public, anon, authenticated;
grant execute on function public.mcp_put_credential(uuid, uuid, text, text, text, timestamptz)
  to service_role;
grant execute on function public.mcp_get_credential(uuid, text) to service_role;
grant execute on function public.mcp_drop_credential(uuid, text) to service_role;

-- A credential's secret goes with its server.
create or replace function public.mcp_credentials_cleanup()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if old.vault_secret_id is not null then
    execute 'delete from vault.secrets where id = $1' using old.vault_secret_id;
  end if;
  return old;
end;
$$;

create trigger mcp_credentials_cleanup
  after delete on public.mcp_credentials
  for each row execute function public.mcp_credentials_cleanup();

revoke execute on function public.mcp_credentials_cleanup() from public, anon, authenticated;
