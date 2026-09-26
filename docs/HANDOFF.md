# Handoff: where Pantheon stands and what to do next

Written 2026-09-25 for a fresh session (the owner is moving to Claude Opus 5.5).
Read this, then `CLAUDE.md`, then `docs/BUILD_PLAN.md`. This file records state
the repo cannot: what is deployed, what was applied to live services, and the
owner's working preferences.

## The one-paragraph state

Steps 0 to 4 are **done and live**. **Step 5** (TypeSafe Jev: judge core,
brain write gate, screening and guardrails, calibration; ADRs 009 to 012)
and **Step 6** (real embeddings, agent creation API, scoped documents, links
preview then push; ADRs 013 to 016) are **built and tested but not live**.
The owner said go past the Step 5 Milestone on 2026-09-26, with the live
eval still owed. The right-hand design (`docs/design/right-hand.md`, seven
ideas) is folded into Steps 7.5 to 11. Owner content rule (2026-09-26):
English only everywhere except the website's vetted verses. **Step 7**
(tool calling, tools, tasks and delegation, runners, approvals and the
decision desk, autonomy and limits; ADRs 017 to 023, including pause versus kill) is **built and tested
but not live**; the Step 7 Milestone was reported and nothing past it
starts until the owner says go. **Step 8.0 and 8.1** (charters, Research and
Intelligence; ADR 024) are built and tested; waiting on the owner's sources and
a deploy for the five live mornings. **Step 7.7** (MCP tools; ADR 025) is built
and tested, with live web search (ADR 026). Merged to `main` 2026-09-26. 467 tests pass.

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
| Migrations applied to Supabase | all of `supabase/migrations/` through `20260926150000_tool_settings.sql` (2026-09-26, owner's go) |
| Supabase Vault | holds MCP server credentials (checked live 2026-09-26: stored encrypted, read back, removed) |
| Seeded live (2026-09-26) | 3 department charters (research final; executive and marketing drafts; not applied), 9 starter Jev gates (32 questions), 6 tools, Jev price, embedding model `openai/text-embedding-3-small`. Not yet: `scripts.brain reembed` (3 old facts use the hashing stand-in and are invisible to search until then) |
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
- Tests (365 passing after Step 6):
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
  logging, tool calling, embeddings, Jev).
- `api/app/judge/` gates; `api/app/tools/` registry and runtime (autonomy,
  risk gate, holds); `api/app/tasks/` delegation; `api/app/approvals/`
  decisions and the decision desk; `api/app/agents/deep.py` the deepagents
  runner, `autonomy.py` the ladder.
- `api/app/brain/` facts, embeddings, search. `api/app/agents/` `research.py`
  (the graph), `runs.py` (leased, resumable runs), `prompts.py`, `triggers.py`,
  `starter_prompts.py`. `api/app/internal.py` the endpoint the scheduler calls.
- `supabase/migrations/` versioned SQL (RLS on every table). `docs/adr/` ADRs
  0001 to 0027. `docs/BUILD_PLAN.md` is the source of truth for order.

## What to do next

1. Migrations and seed are live (2026-09-26). Still owed, against the pooler
   with keys: `scripts.brain reembed` (moves the 3 old facts to the real
   embedding model; until then they are invisible to search) and
   `scripts.judge_eval import`. The API on Vercel deploys from `main`, which
   does not have Steps 5 to 7 yet: merge `step-3` for them to run live.
2. Vercel `pantheon-api` needs `TYPESAFE_API_KEY` and
   `SUPABASE_SERVICE_ROLE_KEY` (legacy key or a new `sb_secret_...` key).
3. In a session with the TypeSafe key: `scripts.jev_smoke`, then
   `scripts.judge_eval run all --repeats 3`, and report the numbers.
4. Open decision 14 (label sources) before building the labeller.
5. Step 8.1 live: research charter v3 (live, 2026-09-26; v3 adds web search) has the owner's first
   topic, space tech VC investment, and five checked sources (SpaceNews business,
   Payload, TechCrunch space, Space Capital publications, Space Insider). With the
   API deployed from Steps 5 to 8: `apply research`, then `enable research`; after five weekday mornings,
   `scripts.department report research` is the Milestone report.
6. MCP (Step 7.7): set `PUBLIC_API_URL` in Vercel to the API's address, deploy,
   then `scripts.mcp add higgsfield <its MCP URL>` and `scripts.mcp connect
   higgsfield` (opens the sign-in; a bearer token goes in `MCP_TOKEN` instead),
   `scripts.mcp tools`, `approve`, `assign`.

7. Switch-on for the morning brief (owner's go, 2026-09-26; PRs #8 and #9
   merged). Needs a session with Full network access (the pooler is not
   HTTPS) and `DATABASE_URL` set to the pooler URL (`SUPABASE_POOLER_URL`
   holds it), and the owner approving each live change:
   - apply `20260926160000_newsletter.sql`; `scripts.agent seed` (adds the
     `route_order` and `brief_rank` gates);
   - `scripts.department sources research --days mon-sat` (keeps the topic
     and sources), `apply research`, `enable research`;
   - `scripts.department publish executive --starter`, `apply executive`,
     `enable executive`;
   - `scripts.mailing set morning-brief --from "The Unreal Lab
     <newsletter@theunreallab.com>" --to ishanpanchaal@theunreallab.com --auto`
     (owner confirmed both addresses and "without approval", 2026-09-26; turn
     `--no-auto` back on before adding anyone else);
   - Vercel `pantheon-api`: `RESEND_API_KEY`, then redeploy.

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
