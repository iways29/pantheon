-- Memory that decides for itself (owner, 2026-09-27; ADR 034).
--
-- What the owner says to the agents is sorted by Jev (the `memory_triage`
-- gate) into a kind of memory. Facts carry the kind:
--   fact        what the brain has always held
--   preference  how the owner wants things done ("call me boss"); every
--               agent reads the active ones before it works
--   rule        a decision or standing rule the owner gave
--   interest    something the owner asked about or cares about
--   style       how the owner works or writes

alter table public.facts
  add column kind text not null default 'fact'
    check (kind in ('fact', 'preference', 'rule', 'interest', 'style'));

create index facts_kind_idx on public.facts (org_id, kind) where status = 'active';

comment on column public.facts.kind is
  'fact, or a memory of the owner''s: preference, rule, interest, style (ADR 034).';
