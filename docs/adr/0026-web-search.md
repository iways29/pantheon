# ADR 026: Live web search through the gateway

- **Status:** Accepted (owner, 2026-09-26)
- **Date:** 2026-09-26
- **Step:** 7.7 follow-up; used by Research (8.1)

## Context

Decision 9 chose plain HTTP fetching of pages the owner names: agents could
read a page, not find one. The owner wants live web search too.

## Decision

**A built-in `web_search` tool (R2) that calls OpenRouter's web plugin
through the gateway.** No new vendor, no new key: searches are paid from the
existing OpenRouter credits, and every search is an ordinary gateway call,
so the pause switch, budgets, tier routing and cost logging apply and the
cost lands on the agent that searched (docs read 2026-09-26).

- **Settings are data** (`tools.settings`, a new column, audited): engine,
  mode, number of results, allowed and excluded sites, and the short
  instruction the search call uses. Starting values: engine `parallel`, mode
  `fast` (about $0.001 a search, English), five results. A daily cap
  (`max_calls_per_day`, 40 to start) bounds the spend.
- **Results are untrusted:** titles, links and excerpts are screened
  (`content_screen`); anything not clean is withheld, only the links remain.
- **Search finds, the write gate decides.** A search result never enters the
  brain directly. The agent reads a result page with `web_fetch_preview` and
  pushes it through the write gate, exactly as for the owner's own sources.
- At L1, R2 tools go through the tool-risk gate first.

Research's web researcher now reads the owner's sources and then searches
each topic (at most three searches a morning), in its charter.

**Company knowledge** needs nothing new: documents the owner uploads
(Step 6, scoped company, department or agent) are what `read_document`
searches.

## Consequences

- A morning with three searches costs about $0.003 plus the tokens of three
  short calls; the cap stops runaway use.
- Changing the engine (Exa, Perplexity, native) or the sites is a settings
  edit; the Control Center will show it with the tool.
