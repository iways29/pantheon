-- The brain as a globe (Step 10, owner's choice 2026-09-27; ADR 036).
--
-- Facts sit inside a ball, not on a disc: a third coordinate for each fact
-- and neighbourhood, and a third axis of the projection. Additive: a map
-- drawn before this keeps working (z is read as 0) until the owner redraws
-- it with `scripts.brain layout --reset`.

alter table public.fact_positions
  add column z real check (z between -1.5 and 1.5);

alter table public.brain_neighbourhoods
  add column z real not null default 0;

alter table public.brain_layouts
  add column axis_z vector(1536);
