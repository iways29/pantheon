-- Step 6: links in two steps, preview then push (ADR 016).
--
-- Previewing a link fetches the page, screens it and shows the facts it would
-- create, and writes nothing to the brain. The preview is kept here so that
-- pushing it later works on exactly what was shown, not on a re-fetch that
-- may have changed.

create table public.link_previews (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  -- The agent that read the page and proposed the claims; it pays for both.
  agent_id uuid not null,
  url text not null,
  final_url text not null,
  content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
  -- The extracted page text: the evidence each pushed claim is checked
  -- against.
  text text not null,
  -- The screening label (Step 5.3). Only a clean page gets claims.
  label text not null check (label in ('clean', 'review', 'quarantined')),
  reasons jsonb not null default '[]'::jsonb,
  claims jsonb not null default '[]'::jsonb,
  status text not null default 'previewed' check (status in ('previewed', 'pushed')),
  -- What the write gate decided for each claim, once pushed.
  results jsonb,
  pushed_at timestamptz,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- Previewing the same page content again returns the same preview.
  unique (org_id, final_url, content_sha256),
  foreign key (agent_id, org_id) references public.agents (id, org_id) on delete cascade
);

comment on table public.link_previews is
  'A fetched, screened page and the claims it would add. Nothing reaches the '
  'brain until the preview is pushed, which is a separate, explicit action.';

create trigger link_previews_set_updated_at
  before update on public.link_previews
  for each row execute function public.set_updated_at();

alter table public.link_previews enable row level security;

create policy link_previews_select on public.link_previews
  for select to authenticated using (public.is_org_member(org_id));
create policy link_previews_insert on public.link_previews
  for insert to authenticated with check (public.is_org_member(org_id));
create policy link_previews_update on public.link_previews
  for update to authenticated
  using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

grant select, insert, update on public.link_previews to authenticated;
grant select, insert, update, delete on public.link_previews to service_role;
