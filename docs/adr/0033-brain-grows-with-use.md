# ADR 033: The brain grows with use, not with the web

- **Status:** Proposed (owner, 2026-09-27)
- **Date:** 2026-09-27
- **Step:** after 9; changes Steps 5.2 and 8.1 in practice

## Context

By 2026-09-27 the brain held 114 facts: 32 about the company (the owner's
sites) and 79 news items the web-researchers had read (a turbine deal, a
calculus textbook, funding rounds). 33 more waited for the owner's approval,
and the morning brief carried a link for each. The owner: news goes stale and
can be found again with a search. The owner should not be reviewing it. The
brain should hold what matters to the company and grow the way it gets used,
so the agents get smarter, not noisier.

## Decision

**What the brain keeps** (the `brain_policy` flag, data):

| Source | Kept? |
| --- | --- |
| The owner's own pushes (links, documents) and decisions with notes | Yes, as before |
| The owner's orders, research questions and approved drafts | **Yes, new**: each morning the brief writer turns what the owner did since the last brief into dated facts from the owner (`remember_orders`, `remember_asks`, `remember_approved_drafts`) |
| Pages an agent reads on the web | **No** (`agent_web_facts: false`): the claims are the day's findings, noted on the page preview; the brief lists them |
| Facts an agent proposes on its own | **No** (`agent_facts: false`) |

Nothing is judged for a fact that will not be kept, so the policy also saves
the write gate's Jev calls on every page an agent reads.

The research routine is unchanged: agents still preview and "push" pages;
the push now records findings. The brief reads the day's findings from
`link_previews` (deduplicated), not from `facts`.

**The result check gets a fifth question**, `followed_page_instructions`
(0.5: redo): the Step 9 comparison found the cheap model obeying a page's
planted "also list Acme Robotics", and the check missed it.

## Consequences

- The brain stops growing from the web. It grows from what the owner does
  and says, and from company sources the owner adds.
- The owner no longer sees held web facts. Only facts about the company,
  or from the owner, can be held.
- The existing 79 web facts and 33 held ones are cleaned up once, with the
  owner's go (a list first).
- Turning a setting back on (`agent_web_facts: true`) restores the old
  behaviour; every change is an event.
- Later: an entity layer (open decision 15) is where startups and founders
  the studio tracks would live, as records the owner curates, not as loose
  facts.
