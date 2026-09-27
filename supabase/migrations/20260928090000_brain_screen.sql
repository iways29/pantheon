-- The brain screen (Step 10, ADR 036).
--
-- 1. Realtime: `events` joins the `supabase_realtime` publication, so the
--    brain screen hears each event as it is written. Realtime applies the
--    `events_select` policy per subscriber: a member hears only their org.
-- 2. Where each fact sits on the screen. Positions come from the facts'
--    embeddings, projected once to two dimensions (`brain_layouts` keeps the
--    projection); a new fact is placed beside its nearest neighbours, and a
--    fact on a new topic at the rim. A placed fact never moves.
-- 3. Neighbourhoods: groups of facts that sit together, each with a short
--    name (from the librarian's model call, else from the graph, else from a
--    claim).
-- The settings are data: the `brain_layout` flag, audited.

do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (
       select 1 from pg_publication_tables
        where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'events'
     ) then
    alter publication supabase_realtime add table public.events;
  end if;
end;
$$;

-- The projection: new facts on a new topic are placed with it.
create table public.brain_layouts (
  org_id uuid primary key references public.orgs (id) on delete cascade,
  mean vector(1536) not null,
  axis_x vector(1536) not null,
  axis_y vector(1536) not null,
  -- Divides projected coordinates so the fitted facts fill the unit disc.
  spread real not null check (spread > 0),
  embedding_model text,
  fact_count integer not null default 0,
  fitted_at timestamptz not null default now()
);

create table public.brain_neighbourhoods (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  x real not null,
  y real not null,
  label text not null default '' check (length(label) <= 60),
  -- Where the name came from: the librarian's model call, the graph's
  -- commonest thing, or the start of a claim.
  label_source text not null default 'claim' check (label_source in ('model', 'entity', 'claim')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, org_id)
);

create table public.fact_positions (
  fact_id uuid primary key,
  org_id uuid not null references public.orgs (id) on delete cascade,
  x real not null check (x between -1.5 and 1.5),
  y real not null check (y between -1.5 and 1.5),
  neighbourhood_id uuid,
  -- fit: the first projection; neighbours: beside its nearest facts;
  -- edge: a new topic, at the rim.
  placed_by text not null check (placed_by in ('fit', 'neighbours', 'edge')),
  created_at timestamptz not null default now(),
  foreign key (fact_id, org_id) references public.facts (id, org_id) on delete cascade,
  foreign key (neighbourhood_id, org_id)
    references public.brain_neighbourhoods (id, org_id) on delete set null (neighbourhood_id)
);

create index fact_positions_org_idx on public.fact_positions (org_id);
create index fact_positions_neighbourhood_idx on public.fact_positions (neighbourhood_id);

create trigger brain_neighbourhoods_set_updated_at
  before update on public.brain_neighbourhoods
  for each row execute function public.set_updated_at();

alter table public.brain_layouts enable row level security;
alter table public.brain_neighbourhoods enable row level security;
alter table public.fact_positions enable row level security;

create policy brain_layouts_select on public.brain_layouts
  for select to authenticated using (public.is_org_member(org_id));
create policy brain_layouts_write on public.brain_layouts
  for all to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));
create policy brain_neighbourhoods_select on public.brain_neighbourhoods
  for select to authenticated using (public.is_org_member(org_id));
create policy brain_neighbourhoods_write on public.brain_neighbourhoods
  for all to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));
create policy fact_positions_select on public.fact_positions
  for select to authenticated using (public.is_org_member(org_id));
create policy fact_positions_write on public.fact_positions
  for all to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

grant select, insert, update on public.brain_layouts, public.brain_neighbourhoods,
  public.fact_positions to authenticated;
grant select, insert, update, delete on public.brain_layouts, public.brain_neighbourhoods,
  public.fact_positions to service_role;

-- How facts are placed: data, audited.
insert into public.system_flags (org_id, key, value)
select id, 'brain_layout',
       '{"neighbours": 3, "new_topic_distance": 0.45, "jitter": 0.03,
         "max_neighbourhoods": 24, "names_per_run": 3}'::jsonb
  from public.orgs
on conflict (org_id, key) do nothing;

create or replace function public.audit_brain_layout_flag()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.key <> 'brain_layout'
     or (tg_op = 'UPDATE' and new.value is not distinct from old.value) then
    return new;
  end if;
  insert into public.events (org_id, type, payload)
  values (new.org_id, 'brain_layout_changed',
          jsonb_build_object('value', new.value, 'changed_by', auth.uid(),
                             'db_role', current_user));
  return new;
end;
$$;

revoke execute on function public.audit_brain_layout_flag() from public, anon, authenticated;

create trigger system_flags_audit_brain_layout
  after insert or update on public.system_flags
  for each row execute function public.audit_brain_layout_flag();
