-- Step 5.2: the brain write gate. No fact enters the brain unjudged.
--
-- ADR 010. Every new fact passes code checks and a TypeSafe judgment before
-- it is written (app/brain/write_gate.py). This migration makes that
-- enforceable rather than a convention: a fact must name the judgment that
-- admitted it, and the database checks that judgment exists.

-- The same normalisation the gate uses for its exact-duplicate check: case,
-- runs of whitespace and trailing punctuation do not make a new fact.
create or replace function public.normalize_claim(p_claim text)
returns text
language sql
immutable
parallel safe
set search_path = ''
as $$
  select lower(regexp_replace(regexp_replace(btrim(p_claim), '\s+', ' ', 'g'), '[\s.!;:]+$', ''));
$$;

alter table public.facts
  -- The judgments.request_id of the brain_claim judgment that let this fact
  -- in. Required for every fact written from now on (trigger below); facts
  -- written before the gate existed keep NULL.
  add column admitted_by uuid,
  -- For claims likely to change (a price, a headcount): when to check again.
  add column review_after date,
  -- Public content may only use facts cleared as public. Everything starts
  -- internal; clearing a fact for public use is a separate, deliberate step.
  add column visibility text not null default 'internal'
    check (visibility in ('public', 'internal')),
  -- The exact words in the source that support the claim, when known.
  add column quote text;

comment on column public.facts.admitted_by is
  'judgments.request_id of the brain_claim judgment that admitted this fact.';

-- The exact-duplicate lookup, on every proposed fact.
create index facts_org_normalized_claim_idx
  on public.facts (org_id, public.normalize_claim(claim));

-- A new fact must point at a real brain_claim judgment in its own org. This
-- is what makes "no bypass" true: code that skips the gate has no judgment to
-- name.
--
-- SECURITY DEFINER so the existence check is not itself filtered by RLS: it
-- runs before the insert policy, and a check the writer could not see through
-- would report a cross-org write as a missing judgment instead of letting the
-- policy refuse it. It reads one fact's own org and returns nothing.
create or replace function public.facts_require_admission()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.admitted_by is null then
    raise exception 'A fact needs the judgment that admitted it (the brain write gate)'
      using errcode = 'check_violation';
  end if;
  if not exists (
    select 1 from public.judgments j
     where j.request_id = new.admitted_by
       and j.org_id = new.org_id
       and j.gate = 'brain_claim'
  ) then
    raise exception 'Fact admitted by % names no brain_claim judgment in this org', new.admitted_by
      using errcode = 'check_violation';
  end if;
  if new.status not in ('active', 'disputed') then
    raise exception 'A new fact starts active or disputed, not %', new.status
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

create trigger facts_require_admission
  before insert on public.facts
  for each row execute function public.facts_require_admission();

revoke execute on function public.facts_require_admission() from public, anon;

-- Held claims wait in the approval queue. A retried agent step must not queue
-- the same claim twice, so approvals gain an idempotency key.
alter table public.approvals
  add column idempotency_key text;

create unique index approvals_org_idempotency_key
  on public.approvals (org_id, idempotency_key) where idempotency_key is not null;
