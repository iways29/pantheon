-- Step 6: real embeddings through the gateway (ADR 013).
--
-- Until now every fact was embedded by a free word-hashing stand-in. The owner
-- chose (2026-09-26) a real embedding model through OpenRouter, picked in the
-- database like the model tiers (ADR 003), so it can be changed without a
-- deploy.
--
-- Vectors from two different models cannot be compared, so each embedded row
-- records the model that embedded it, and search only compares rows embedded
-- by the model in use. After switching models, `python -m scripts.brain
-- reembed` brings older rows across.

-- The embedding model is assigned like a tier: org-wide, or per department.
-- Agents never select it; the gateway resolves it for whoever is embedding.
alter table public.model_tier_assignments
  drop constraint model_tier_assignments_tier_check,
  add constraint model_tier_assignments_tier_check
    check (tier in ('cheap', 'standard', 'frontier', 'embedding'));

alter table public.facts
  add column embedding_model text;

comment on column public.facts.embedding_model is
  'The model that produced `embedding`. Search compares only rows embedded by '
  'the model in use; `hashing-v1` is the free stand-in used before Step 6.';

-- Everything embedded so far came from the stand-in.
update public.facts set embedding_model = 'hashing-v1' where embedding is not null;

create index facts_org_embedding_model_idx on public.facts (org_id, embedding_model);

-- The owner's choice for every existing org: OpenAI's text-embedding-3-small,
-- through OpenRouter. 1536 dimensions, the width of the vector columns, at
-- $0.02 per million tokens (OpenRouter catalogue, read 2026-09-26).
insert into public.model_tier_assignments (org_id, department_id, tier, model)
select o.id, null, 'embedding', 'openai/text-embedding-3-small'
  from public.orgs o
on conflict on constraint model_tier_assignments_scope_key do nothing;
