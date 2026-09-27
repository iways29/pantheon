# ADR 035: The graph: things and links, with Jev deciding who is who

- **Status:** Proposed (owner, 2026-09-27)
- **Date:** 2026-09-27
- **Step:** after 9; piece 2a of the brain plan (piece 1 is ADR 034)

## Context

The brain holds facts. The owner wants it to be a graph: the people,
companies, products and projects the facts are about, linked, so a question
can follow the links. A fact that arrives days later must attach to what is
already there, not start a separate cluster. Two things with one name (a
Saudi startup called Think and a US robotics firm called Think) must stay
two. Neo4j or another graph database would be a new paid service; the links
fit in Postgres (option A).

## Decision

**Three tables** (migration `20260927140000_graph.sql`, RLS, `org_id`):
`entities` (a thing: kind, name, other names, description, embedding),
`entity_links` (a typed link, each traced to the fact it came from) and
`fact_entities` (which things each fact mentions). The allowed kinds and
relations are data (the `graph_schema` flag).

**Who does what:**

| Job | Who |
| --- | --- |
| Read a fact: which things, which links | The librarian, a cheap-tier agent; prompt `extract_graph` in the database |
| Which existing thing a mention is, or a new one | Jev, `entity_match`: the candidates (same name, other name, or near by meaning) become the options of one Choice question |
| Does the fact state the link | Jev, `link_support` (fails closed: no link without it) |
| What is kept | Rules as data: join at 0.7 or more; 0.4 to 0.7 is a new thing marked "possible match"; below, a new thing |

**No separate clusters.** Every mention is matched on arrival, so a fact
five days later joins the things already there. A **tidy-up** at the end of
each run puts probable twins to Jev again (marked pairs, overlapping names,
very near by meaning) and merges them when it is sure. An unsure answer
keeps the mark for the next run; only a sure "different" clears it. A merge
keeps the old row, its fact rows and its retired links, pointing at the
survivor, so a wrong merge can be undone. Things with no links are counted
in each run's output.

**When:** the "Brain librarian" routine, daily at 07:40 New York (after
the brief), created switched off, $0.25/day budget, 10 facts per run.

## Consequences

- Well under a cent per fact (one cheap call, one Jev call per matched
  thing and per link); measured after the first runs.
- Searching along the links (piece 2b, one or two hops in `brain_search`)
  comes only after an eval shows it finds more.
- 14 labelled `entity_match` cases, tricky names included
  (`scripts.judge_eval run entity_match`).
- Moving to a graph database later means copying three tables.
