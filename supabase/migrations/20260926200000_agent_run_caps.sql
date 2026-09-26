-- Per-agent run caps (owner, 2026-09-26).
--
-- A task handed to an agent used the table default (50,000 tokens, 25 steps).
-- A web researcher's tool loop re-reads its whole conversation each turn and
-- hit that cap after a few pages. The caps are now charter data per agent;
-- NULL keeps the defaults. Changing them is an agent update, audited like the
-- rest of the agent.

alter table public.agents
  add column max_run_tokens integer check (max_run_tokens between 1000 and 500000),
  add column max_run_steps integer check (max_run_steps between 1 and 100);

comment on column public.agents.max_run_tokens is
  'Token cap for each task handed to this agent; NULL: the default (50,000).';
comment on column public.agents.max_run_steps is
  'Step cap for each task handed to this agent; NULL: the default (25).';
