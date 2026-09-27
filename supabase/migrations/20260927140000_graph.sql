-- The graph: things and the links between them (owner, 2026-09-27; ADR 035).
--
-- Facts stay the unit of knowledge. On top of them the brain records the
-- things they are about (people, companies, products, projects, investors,
-- funds, topics) and the links between those things, each link traced to the
-- fact it came from. The librarian (a cheap-tier agent) proposes things and
-- links from new facts; Jev decides which existing thing a mention is and
-- whether the fact supports a link; rules held as data decide what is kept.
--
-- - `entities`: one row per thing. A mention that matches an existing thing
--   joins it (its name becomes an alias); a merge keeps the old row, pointing
--   at the survivor (`merged_into`), so a wrong merge can be undone.
-- - `entity_links`: typed, directed links, each with the fact behind it.
-- - `fact_entities`: which things each fact mentions: how a fact that arrives
--   days later attaches to the things already there.
-- - `facts.graph_state`: pending (new, to read), done, skipped.
-- - The allowed kinds and relations are the `graph_schema` flag (data).
-- - The librarian is a new runner.

alter table public.facts
  add column graph_state text not null default 'pending'
    check (graph_state in ('pending', 'done', 'skipped'));
create index facts_graph_pending_idx on public.facts (org_id, created_at)
  where graph_state = 'pending' and status = 'active';

create table public.entities (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  kind text not null check (kind ~ '^[a-z][a-z_]{1,30}$'),
  name text not null check (length(btrim(name)) between 1 and 200),
  aliases text[] not null default '{}',
  description text not null default '' check (length(description) <= 1000),
  embedding vector(1536),
  embedding_model text,
  status text not null default 'active' check (status in ('active', 'merged', 'retired')),
  merged_into uuid,
  -- Kept apart when Jev was unsure it matched an existing thing: the tidy-up
  -- looks again.
  possible_match_of uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, org_id),
  foreign key (merged_into, org_id) references public.entities (id, org_id),
  foreign key (possible_match_of, org_id) references public.entities (id, org_id)
);

create index entities_org_name_idx on public.entities (org_id, lower(name)) where status = 'active';
create index entities_embedding_idx on public.entities using hnsw (embedding vector_cosine_ops);

create table public.entity_links (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  from_entity uuid not null,
  relation text not null check (relation ~ '^[a-z][a-z_]{1,30}$'),
  to_entity uuid not null,
  -- The fact the link was read from; a link without one is not kept.
  fact_id uuid not null,
  -- Jev's yes-probability that the fact states the link.
  confidence numeric(4, 3) check (confidence between 0 and 1),
  status text not null default 'active' check (status in ('active', 'retired')),
  created_at timestamptz not null default now(),
  foreign key (from_entity, org_id) references public.entities (id, org_id) on delete cascade,
  foreign key (to_entity, org_id) references public.entities (id, org_id) on delete cascade,
  foreign key (fact_id, org_id) references public.facts (id, org_id) on delete cascade,
  unique (org_id, from_entity, relation, to_entity, fact_id),
  check (from_entity <> to_entity)
);

create index entity_links_from_idx on public.entity_links (from_entity) where status = 'active';
create index entity_links_to_idx on public.entity_links (to_entity) where status = 'active';

create table public.fact_entities (
  org_id uuid not null references public.orgs (id) on delete cascade,
  fact_id uuid not null,
  entity_id uuid not null,
  created_at timestamptz not null default now(),
  primary key (fact_id, entity_id),
  foreign key (fact_id, org_id) references public.facts (id, org_id) on delete cascade,
  foreign key (entity_id, org_id) references public.entities (id, org_id) on delete cascade
);

create index fact_entities_entity_idx on public.fact_entities (entity_id);

create trigger entities_set_updated_at
  before update on public.entities
  for each row execute function public.set_updated_at();

-- Every change to a thing is an event: created, merged, retired.
create or replace function public.audit_entity()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE' and new.status = old.status and new.merged_into is not distinct from old.merged_into then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'entity_' || case when tg_op = 'INSERT' then 'created' else new.status end,
          jsonb_build_object('entity_id', new.id, 'kind', new.kind, 'name', new.name,
                             'merged_into', new.merged_into));
  return new;
end;
$$;

revoke execute on function public.audit_entity() from public, anon, authenticated;

create trigger entities_audit
  after insert or update on public.entities
  for each row execute function public.audit_entity();

alter table public.entities enable row level security;
alter table public.entity_links enable row level security;
alter table public.fact_entities enable row level security;

create policy entities_select on public.entities
  for select to authenticated using (public.is_org_member(org_id));
create policy entities_write on public.entities
  for all to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));
create policy entity_links_select on public.entity_links
  for select to authenticated using (public.is_org_member(org_id));
create policy entity_links_write on public.entity_links
  for all to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));
create policy fact_entities_select on public.fact_entities
  for select to authenticated using (public.is_org_member(org_id));
create policy fact_entities_write on public.fact_entities
  for all to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

grant select, insert, update on public.entities, public.entity_links, public.fact_entities
  to authenticated;
grant select, insert, update, delete on public.entities, public.entity_links, public.fact_entities
  to service_role;

-- The kinds and relations the librarian may use: data, audited.
insert into public.system_flags (org_id, key, value)
select id, 'graph_schema',
       '{"kinds": ["person", "company", "product", "project", "investor", "fund", "topic"],
         "relations": ["founded", "works_on", "invested_in", "competes_with", "part_of",
                       "decided_about", "prefers"]}'::jsonb
  from public.orgs
on conflict (org_id, key) do nothing;

create or replace function public.audit_graph_schema_flag()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.key <> 'graph_schema'
     or (tg_op = 'UPDATE' and new.value is not distinct from old.value) then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'graph_schema_changed',
          jsonb_build_object('value', new.value, 'changed_by', auth.uid(),
                             'db_role', current_user));
  return new;
end;
$$;

revoke execute on function public.audit_graph_schema_flag() from public, anon, authenticated;

create trigger system_flags_audit_graph_schema
  after insert or update on public.system_flags
  for each row execute function public.audit_graph_schema_flag();

-- The librarian runs its own code, like the router and the digest.
alter table public.agents drop constraint agents_runner_check;
alter table public.agents
  add constraint agents_runner_check
    check (runner in ('pipeline', 'deep', 'router', 'digest', 'librarian'));
