-- LangGraph checkpoints, in a private schema, with org_id and RLS.
--
-- ADR 005. The library's own setup() would create these tables in `public`
-- with no org_id and no RLS, where Supabase's Data API could reach them. So
-- the tables are created here instead, as a versioned migration, and setup()
-- is never called.
--
-- The shape is copied from langgraph-checkpoint-postgres 3.1.2, the final
-- state after its MIGRATIONS list, and that version is pinned exactly in
-- api/pyproject.toml. Upgrading the library means diffing its MIGRATIONS
-- against this file and adding a new migration for anything that changed.
-- The library's own bookkeeping table, checkpoint_migrations, is not created:
-- only setup() reads it.
--
-- The checkpointer connects with `role=service_role` and
-- `search_path=langgraph,public` as connect-time options, so the library's
-- unqualified table names resolve here. See app/agents/checkpointer.py.

create schema langgraph;

comment on schema langgraph is
  'LangGraph checkpoint state. Not exposed through the Data API; only the '
  'backend, as service_role, reads or writes it.';

revoke all on schema langgraph from public;
grant usage on schema langgraph to service_role;

-- Every checkpoint belongs to a run: the graph's thread_id is the run's id.
-- The library never supplies org_id, so this fills it from the run, and a
-- thread that is not a run is refused rather than stored unowned.
create function langgraph.set_org_id_from_run()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  select r.org_id into new.org_id
  from public.runs r
  where r.id = new.thread_id::uuid;

  if new.org_id is null then
    raise exception 'checkpoint thread % is not a run', new.thread_id
      using errcode = 'foreign_key_violation';
  end if;

  return new;
end;
$$;

create table langgraph.checkpoints (
  thread_id text not null,
  checkpoint_ns text not null default '',
  checkpoint_id text not null,
  parent_checkpoint_id text,
  type text,
  checkpoint jsonb not null,
  metadata jsonb not null default '{}',
  org_id uuid not null references public.orgs (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (thread_id, checkpoint_ns, checkpoint_id)
);

create table langgraph.checkpoint_blobs (
  thread_id text not null,
  checkpoint_ns text not null default '',
  channel text not null,
  version text not null,
  type text not null,
  blob bytea,
  org_id uuid not null references public.orgs (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (thread_id, checkpoint_ns, channel, version)
);

create table langgraph.checkpoint_writes (
  thread_id text not null,
  checkpoint_ns text not null default '',
  checkpoint_id text not null,
  task_id text not null,
  idx integer not null,
  channel text not null,
  type text,
  blob bytea not null,
  task_path text not null default '',
  org_id uuid not null references public.orgs (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

create index checkpoints_thread_id_idx on langgraph.checkpoints (thread_id);
create index checkpoint_blobs_thread_id_idx on langgraph.checkpoint_blobs (thread_id);
create index checkpoint_writes_thread_id_idx on langgraph.checkpoint_writes (thread_id);
create index checkpoints_org_id_idx on langgraph.checkpoints (org_id);

create trigger checkpoints_set_org_id
  before insert on langgraph.checkpoints
  for each row execute function langgraph.set_org_id_from_run();
create trigger checkpoint_blobs_set_org_id
  before insert on langgraph.checkpoint_blobs
  for each row execute function langgraph.set_org_id_from_run();
create trigger checkpoint_writes_set_org_id
  before insert on langgraph.checkpoint_writes
  for each row execute function langgraph.set_org_id_from_run();

-- RLS on, with a read policy for org members and nothing else. Only
-- service_role holds table privileges today, and it bypasses RLS, so these
-- policies change nothing yet; they are what makes a future grant to
-- authenticated (a run inspector in the UI, say) safe by default.
alter table langgraph.checkpoints enable row level security;
alter table langgraph.checkpoint_blobs enable row level security;
alter table langgraph.checkpoint_writes enable row level security;

create policy checkpoints_select on langgraph.checkpoints
  for select to authenticated using (public.is_org_member(org_id));
create policy checkpoint_blobs_select on langgraph.checkpoint_blobs
  for select to authenticated using (public.is_org_member(org_id));
create policy checkpoint_writes_select on langgraph.checkpoint_writes
  for select to authenticated using (public.is_org_member(org_id));

-- Revoked from anon and authenticated by name as well as from public:
-- Supabase's default privileges grant to those roles explicitly, and a revoke
-- from public leaves such grants in place (see migration 20260921015000).
revoke all on schema langgraph from anon, authenticated;
revoke all on all tables in schema langgraph from public, anon, authenticated;
grant select, insert, update, delete on all tables in schema langgraph to service_role;
revoke execute on function langgraph.set_org_id_from_run() from public, anon, authenticated;
