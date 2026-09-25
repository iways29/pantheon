# Handoff: where Pantheon stands and what to do next

Written 2026-09-25 for a fresh session (the owner is moving to Claude Opus 5.5).
Read this, then `CLAUDE.md`, then `docs/BUILD_PLAN.md`. This file records state
the repo cannot: what is deployed, what was applied to live services, and the
owner's working preferences.

## The one-paragraph state

Steps 0 to 4 are **done and live**. **Step 5 (TypeSafe Jev) is built and
tested but not live** (2026-09-25, ADRs 009 to 012): the judge core (5.1),
the brain write gate (5.2; no fact enters the brain unjudged), content
screening and guardrails (5.3), and the calibration harness with 42 labelled
cases (5.4). 295 tests pass. Decisions 10 and 11 were confirmed by the owner.
**We are at the Step 5 Milestone**: the owner must say go before Step 6.
Not yet done: the live smoke test and the first live eval run (the cloud
session that built Step 5 had no `TYPESAFE_API_KEY`), and applying the four
new migrations to Supabase.

## Working agreement with the owner (keep to it)

- **Stay on branch `step-3`.** No pull requests for now; the owner will push
  everything together later. Commit small, one step at a time. Do not push
  unless asked. (GitHub `main` already has PR #6, which contains everything up
  to Step 4; local `step-3` is ahead of `origin/step-3` by the planning commit
  and whatever you add.)
- **Explain in simple, plain language.** The owner asked for "like I am 10" on
  the first explanation and likes short tables and clear next steps.
- **Never enter secrets or accounts.** Tell the owner exactly where to put a
  value and check later that it exists (names and lengths only, never values).
- **Ask before** outward actions (pushing, opening PRs, applying migrations to
  the live Supabase project, anything that spends real money), and before any
  new paid service or dependency. Applying migrations to Supabase has been
  approved case by case each time.
- **Stop at each Milestone** in the plan (after Step 5, Step 7, Step 8.1).
- The owner's time zone is **America/New_York**. They want a fixed morning
  routine, not agents running all day, and to be able to step in at any time.

## Live services (ids are not secrets; keys are never written here)

| Thing | Value |
| --- | --- |
| GitHub | `iways29/pantheon` |
| Supabase project | `epelwbiehczkkgqtgkqt` (name `pantheon`, us-west-2), reachable through the Supabase MCP |
| Migrations applied to Supabase | all of `supabase/migrations/` through `20260925040100_trigger_schedule.sql` |
| Vercel team | `team_6UC1DSN83JcGLrVJC3P8aXE6` |
| API project | `pantheon-api` (`prj_XHno72KOypj7fapEIXze9XtZZJMR`), production `https://api-xi-opal-67.vercel.app`, deploys from `main` |
| Web project | `pantheon-web` (`prj_XdV67seWTQSsmSDvBUH2NIg9AZNX`) |
| Org / owner | org `Pantheon` `b57083a6-e31a-4b33-bc67-a1c9982e9fb1`; owner user `1c204154-30fe-48bc-bc24-d018b6eed4c2` |
| Seeded | department `research` at $0.25/day, agent `researcher` on the cheap tier, prompts `answer` v1 and `extract` v1 |
| Scheduler | `pg_cron` job `pantheon-tick` runs every minute; Vault has `pantheon_api_url` and `pantheon_trigger_secret`; Vercel has `TRIGGER_SECRET` |

The Vercel MCP cannot list environment variables (403), so you cannot confirm
those; the API's 401 versus 503 answers reveal whether `TRIGGER_SECRET` is set.

## Local setup

- Docker container `pantheon-testdb` (pgvector, Postgres 16): port **55432**,
  databases `pantheon_test` and `pantheon_dev`, app login `pantheon`/`pantheon`,
  superuser `postgres`/`postgres`. Start it with `docker start pantheon-testdb`.
- Tests (297 passing after Step 5):
  `cd api && DATABASE_URL=postgresql://pantheon:pantheon@127.0.0.1:55432/pantheon_test uv run pytest -q`
  then `uv run ruff check .` and `uv run ruff format --check .`.
- **New migrations must be applied by hand** to both local databases, as the
  superuser:
  `docker exec -i pantheon-testdb psql -U postgres -d pantheon_test -v ON_ERROR_STOP=1 -q < supabase/migrations/<file>.sql`
  (repeat for `pantheon_dev`). There is no `supabase` CLI or `psql` on the Mac.
  For the live project use the Supabase MCP `apply_migration`, with the owner's
  approval. A migration must also apply from scratch on a plain Postgres
  (CI does that with `scripts/local_db.sh reset`), so anything needing
  `pg_cron`, `pg_net` or Vault must be guarded (see `20260925040100`).
- The repo-root `.env` has real keys and is git-ignored. Its `DATABASE_URL`
  points at the **local dev database**. To run the CLI against Supabase,
  override it for one command with the pooler URL, taken with `grep`, for
  example:
  `POOLER=$(grep -E '^SUPABASE_POOLER_URL=' .env | cut -d= -f2- | sed -E "s/^['\"]|['\"]$//g")`
  then `DATABASE_URL="$POOLER" uv run python -m scripts.agent <command>`.
  **Never `source .env`**: it strips the quotes from `MODEL_TIERS` and breaks it.
- Owner CLI (`api/scripts/agent.py`): `seed`, `ask`, `resume`, `retry`, `show`,
  `prompt list|set|activate`, `trigger list|add|enable|disable|remove`.
- Check `.env.example` before every commit: the owner once put real keys in it.

## Tooling gotchas

- macOS: `sed -i` needs `''`; foreground `sleep` is blocked (use a background
  `until` loop and wait for its notification); `git rebase` is blocked, so the
  owner runs history rewrites.
- Docs for any external API must be read live before coding (`CLAUDE.md`).
  TypeSafe docs: `https://docs.typesafe.ai/llms.txt`; append `.md` to a page path.
  The plugin `typesafe:typesafe-ai` (a skill) points at them.

## Where the code is

- `api/app/gateway/` the only path to models (tiers, budgets, kill switch, cost
  logging; text only, no tool calling yet: that is Step 7.1).
- `api/app/brain/` facts, embeddings, search. `api/app/agents/` `research.py`
  (the graph), `runs.py` (leased, resumable runs), `prompts.py`, `triggers.py`,
  `starter_prompts.py`. `api/app/internal.py` the endpoint the scheduler calls.
- `supabase/migrations/` versioned SQL (RLS on every table). `docs/adr/` ADRs
  0001 to 0008. `docs/BUILD_PLAN.md` is the source of truth for order.

## What to do next

1. **With the owner's go-ahead**, apply the Step 5 migrations to Supabase
   (`20260925050000` to `20260925080000`) through the Supabase MCP, then run
   `scripts.agent seed` against the pooler to publish the five starter gates
   and the Jev price for the live org, and `scripts.judge_eval import`.
2. In a session with the key: `uv run python -m scripts.jev_smoke`, then
   `uv run python -m scripts.judge_eval run all --repeats 3`. Report the
   numbers to the owner; the owner sets thresholds with `scripts.judge gate set`.
3. Put `TYPESAFE_API_KEY` in the Vercel `pantheon-api` project, or the
   research agent answers but stores no facts (`fact_writes: not_judged`).
4. Open decision 14 (label sources) before building the frontier-model
   labeller. Decisions 5, 12 and 13 come at Steps 7 and 8.
5. Deleting an org used to fail when it had a model tier assignment (the
   audit trigger wrote an event for the deleted org). Fixed by migration
   `20260925080000`; apply it to Supabase with the Step 5 ones.

## Prompt to paste into the new chat

```
Continue the Pantheon project on branch step-3 (do not open a PR or push; I will
push everything together later). Read, in order: CLAUDE.md, docs/HANDOFF.md,
docs/BUILD_PLAN.md (Step 5 onward), docs/research/typesafe-jev.md,
docs/design/agent-organization.md and docs/business/the-unreal-lab.md. Then re-read the live TypeSafe docs
(https://docs.typesafe.ai/llms.txt) and use the typesafe:typesafe-ai skill before
coding. Start Step 5.1 (judge core). First ask me to confirm open decisions 10
(thin httpx transport to TypeSafe) and 11 (no sensitive data to TypeSafe for now)
in one short question, using the recommendations in the plan. I am arranging the
TYPESAFE_API_KEY myself; until it is set, build and test against a scripted
transport. Explain things in simple, plain language, and stop at the Step 5
Milestone.
```
