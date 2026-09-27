# Handoff: where Pantheon stands and what to do next

Refreshed 2026-09-26 (PRs up to #34 merged, all live). Read this after
`CLAUDE.md`, then `docs/BUILD_PLAN.md` (Step 8 onward), `docs/glossary.md` and
`docs/business/the-unreal-lab.md`. This file records what the repo cannot:
what is deployed, what was applied to live services, and how the owner works.

## The one-paragraph state

Steps 0 to 8.3 are **built and live**. Three departments run on their own each
morning: **Research** (scouts early AI startups and founders), **Executive**
(the morning brief and the evening question) and **Marketing** (drafts only,
each an approval card). The Step 8.1 Milestone report
(`scripts.department report research`) is due after **five unattended
mornings**; stop there and report. Next builds, in the owner's order
(2026-09-26): **approval links in the brief email**, then **Step 9 (Jev in the
loop)**. Step 11 (Control Center) comes after.

## What runs each day (New York time)

| When | Who | What |
| --- | --- | --- |
| 06:30 Mon-Sat | Research (charter v12) | Lead splits 6 sources across two web-researchers (L3, 150k-token run caps); keeps only the last 30 days; names up to 3 startups or founders |
| 06:45 Mon-Fri | Marketing (charter v4) | Content Lead, topic researcher, editor (cheap tier), writer (standard). Goals: founder applications and Mumba sign-ups. Drafts only |
| 07:15 Mon-Sat | Executive (charter v5) | The brief by email: findings first, problems grouped, held facts listed, a credit alert, a plain fallback with no model |
| 21:00 Sun-Fri | Executive | The evening question. The owner answers with `scripts.department ask research "..."`; used once the next morning |

Research sources: TechCrunch AI, TechCrunch seed tag, Crunchbase News AI,
Show HN, Launch HN, BetaList.

## Open items for the owner

| Item | Where |
| --- | --- |
| First trial draft `f9f54d54` waits for approval | `scripts.draft show f9f54d54`, then `approve` or `reject` |
| 13 facts held | `scripts.brain held`, then `admit` or `reject` |
| Step 8.1 Milestone report after five mornings | `scripts.department report research` |

## Models and money

| Thing | Value |
| --- | --- |
| Cheap tier | `openai/gpt-6-luna` (`model_tier_assignments`) |
| Standard tier | `gpt-5.6-sol` |
| Reasoning | `system_flags` `reasoning` = low effort for cheap and standard |
| Tool calls | One per turn (no `parallel_tool_calls`) |
| Provider errors | Retried 5 times; after that `scripts.approvals resume --reason upstream_error` |
| OpenRouter credit | $10, topped up 2026-09-26 |

## Brain

32 public facts from the owner's sites, plus research facts.
`docs/business/company-profile.md` is the company document agents read; the
full brief is quarantined by screening (its agent rules read as instructions to
an AI).

## Gotchas (read before touching live)

| Trap | Do this instead |
| --- | --- |
| `department publish research --starter` wipes the live topics and sources | Patch the `show` JSON and publish the file |
| The owner's shell exports an empty `SUPABASE_SERVICE_ROLE_KEY` | Run `scripts.knowledge` with `env -u SUPABASE_SERVICE_ROLE_KEY` |
| `tasks.order` is idempotent on title | Give a rerun a new title |
| `source .env` strips the quotes from `MODEL_TIERS` | Never source it; `grep` single values out |
| `.env` `DATABASE_URL` points at local Docker | Prefix live commands with `DATABASE_URL="$(grep -E '^SUPABASE_POOLER_URL=' ../.env \| cut -d= -f2-)"` |
| `.env.example` once got real keys | Check it before every commit |

## Working agreement with the owner (keep to it)

- **Work reaches `main` only through a pull request the owner merges.** Branch
  from `origin/main`, commit small, open the PR. Never push to `main`.
- **Ask before every live change** (migrations on Supabase, charter publishes,
  anything that spends money, new paid services or dependencies).
- **Plain, simple language with short tables.**
- **Never enter secrets or accounts.** Say where a value goes; check names and
  lengths only.
- **Stop at each Milestone** (the next is Step 8.1's report).
- Time zone **America/New_York**. A fixed morning routine, not agents running
  all day; the owner can step in at any time.

## Live services (ids are not secrets; keys never go here)

| Thing | Value |
| --- | --- |
| GitHub | `iways29/pantheon` |
| Supabase project | `epelwbiehczkkgqtgkqt` (us-west-2), reachable through the Supabase MCP |
| Migrations applied live | all of `supabase/migrations/` through `20260927130000_owner_memory.sql` |
| Vercel team | `team_6UC1DSN83JcGLrVJC3P8aXE6` |
| API project | `pantheon-api` (`prj_XHno72KOypj7fapEIXze9XtZZJMR`), `https://api-xi-opal-67.vercel.app`, deploys from `main` |
| Web project | `pantheon-web` (`prj_XdV67seWTQSsmSDvBUH2NIg9AZNX`) |
| Org / owner | org `b57083a6-e31a-4b33-bc67-a1c9982e9fb1`; owner user `1c204154-30fe-48bc-bc24-d018b6eed4c2` |
| Scheduler | `pg_cron` job `pantheon-tick` every minute; Vault holds `pantheon_api_url` and `pantheon_trigger_secret` |
| Email | Resend; list `morning-brief` sends from `The Unreal Lab <newsletter@theunreallab.com>` to the owner without approval. Turn `--no-auto` back on before adding anyone |
| Higgsfield MCP | Connected; tools named `mcp_higgsfield_*`; generation is R4 (approval, daily cap) |

The Vercel MCP cannot list environment variables (403).

## Tests

547 pass on the `pantheon_ci` database:
`cd api && DATABASE_URL=postgresql://pantheon:pantheon@127.0.0.1:55432/pantheon_ci uv run pytest -q`,
then `uv run ruff check .` and `uv run ruff format --check .`. Rebuild with
`scripts/local_db.sh reset` (`DB_NAME=pantheon_ci`). New migrations must also
apply from scratch on plain Postgres (guard anything needing `pg_cron`,
`pg_net` or Vault). Live migrations go in through the Supabase MCP
`apply_migration`, with the owner's go.

## Where the code is

- `api/app/gateway/` the only path to models; `api/app/judge/` Jev gates;
  `api/app/brain/` facts and search; `api/app/tools/` registry and runtime;
  `api/app/tasks/` delegation; `api/app/approvals/` the decision desk;
  `api/app/agents/` runners and runs; `api/app/departments/` charters;
  `api/app/agents/digest.py` the brief; `api/app/mail/` Resend and the outbox; `api/app/content/` drafts.
- `api/scripts/` the owner's CLIs (`department`, `draft`, `brain`,
  `approvals`, `order`, `judge`, `mailing`, `knowledge`, `mcp`, `agent`).
- `supabase/migrations/` versioned SQL, RLS on every table. `docs/adr/` 0001
  to 0035.

## What to do next

1. Watch Monday's mornings (research 06:30, marketing 06:45, brief 07:15) and
   fix anything that breaks.
2. Done 2026-09-27: approval links in the brief email (ADR 030) are live
   (migration applied, PR #35 merged, links on for `morning-brief`, 48 hours).
3. Step 9 is live (2026-09-27, owner's go after a clean rehearsal): migration
   `20260927110000_result_check.sql` applied, PR #36 merged, gates
   `result_check` and `recall_rank` seeded (v1). First live checks: both
   web-researchers passed on cheap; the fact-curator was wrongly redone for
   stating today's date (fixed in the next PR: the evidence now includes
   today's date and the task). Still owed: `scripts.cascade compare` from the
   owner's laptop, the Step 9 report.
3b. **Step 9 is done** (report: `docs/reports/step9-comparison.md`). Next,
   before Steps 10 and 11 (a new conversation; the owner has designs):
   ADR 033, the brain grows with use. Web reads are the day's findings, not
   facts; the owner's orders, questions and approved drafts are remembered
   each morning; a one-time clean-up of the 79 web facts and 33 held ones,
   with the owner's go.
3c. Brain plan (owner, 2026-09-27). Done: the clean-up (82 web facts deleted,
   33 held rejected; 32 company facts kept). Piece 1, ADR 034: Jev sorts what
   the owner says (memory_triage) and every agent reads the owner's
   preferences. Live 2026-09-27: migration `20260927130000_owner_memory.sql`
   applied, PR #39 merged, `memory_triage` v1 seeded, `brain_claim` v3 (the
   owner's v2 plus the `owner` profile). Piece 2a, ADR 035: the graph (things and links in Postgres;
   the librarian proposes, Jev decides which existing thing a mention is and
   whether a fact states a link; a tidy-up merges twins). Going live: apply
   `20260927140000_graph.sql`, merge, `scripts.judge seed entity_match` and
   `link_support`, `scripts.graph setup`, then the owner runs `scripts.graph on`.
   Piece 2b (search along the links) only after an eval shows it helps.
4. After five unattended mornings: the Step 8.1 Milestone report. Stop.

## Switching on Step 9 (done 2026-09-27; kept for reference)

Everything is off until each gate is added, so this can go step by step.

| # | Step | Who | Undo |
| --- | --- | --- | --- |
| 1 | Apply `20260927110000_result_check.sql` through the Supabase MCP. Additive: new columns, a guard trigger, and the dispatch and task-follows-run functions redefined with the new behaviour | Claude | Leave it; it is inert without the code |
| 2 | Merge PR #36; Vercel deploys | Owner | Revert the PR |
| 3 | `scripts.judge seed result_check`: worker results are checked from the next task | Owner (from `api/`, pooler prefix) | `scripts.judge gate off result_check` |
| 4 | `scripts.judge seed recall_rank`: `brain_search` is re-ranked | Owner | `scripts.judge gate off recall_rank` |
| 5 | `scripts.cascade compare --save ../docs/reports/step9-comparison.md`: the Step 9 report, about 3 cents; creates the `benchmark` agent and `benchmarks` department ($0.50/day) | Owner, or Claude with the owner's go | Nothing to undo |
| 6 | After the next mornings: `scripts.cascade report` | Owner or Claude (SQL) | - |

Step 1 must come before step 2: the new gateway reads `runs.model_tier`.
Run commands from `api/` with the live database, for example:
`DATABASE_URL="$(grep -E '^SUPABASE_POOLER_URL=' ../.env | cut -d= -f2-)" uv run python -m scripts.judge seed result_check`
