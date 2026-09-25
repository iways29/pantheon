# Pantheon

An AI-agent "employee" system with a **brain** at its center: a shared fact house that agents read from and write to. Phase 1 has exactly one operator (the owner). Later phases onboard other companies and expose individual or multiple agents to them. Read `docs/BUILD_PLAN.md` for the ordered build steps and acceptance criteria.

## Non-negotiable priorities

1. **Cost control.** Target is roughly $50-200/month total in phase 1. The owner must never burn $500-1000/month before there is revenue. Every design choice is judged against this.
2. **Future-safe, no big rewrites, but move fast.** Do the cheap groundwork now (multi-tenancy, event log, portable agent code). Do not build phase 2 features yet.
3. **Deterministic, auditable decisions.** Judgments are logged. Irreversible actions need human approval.

## Locked stack (do not swap without asking)

- **Hosting:** Vercel Pro (frontend + Python API as Vercel Functions on Fluid compute). No Fargate, no always-on servers in phase 1.
- **Database:** Supabase Postgres (Pro). One database for facts, vectors (pgvector), LangGraph checkpoints, runs, events, approvals, budgets, judgments. Supabase Auth and Realtime.
- **Agents:** LangGraph and deepagents (LangChain). Python.
- **Models:** OpenRouter only, so different agents can use different models.
- **Judgments:** TypeSafe (Jev, System One models) for typed, probabilistic decisions and fact-checking. Docs: https://docs.typesafe.ai/llms.txt (read the live docs before integrating; never guess API details).
- **Languages:** Python backend (`api/`), TypeScript frontend (`web/`).
- **Not doing:** local models, self-hosted GPUs, Hermes as a runtime, anything that needs the owner's laptop to stay on.

## Architecture in one screen

```
web/ (Next.js, Vercel)  <-- Supabase Realtime on `events` -->  brain UI + HOD chat
        |
api/ (FastAPI on Vercel Functions)  -- auth, chat, trigger endpoints, SSE streaming
        |
        +-- agents/   LangGraph + deepagents; short resumable runs, checkpoint in Postgres
        +-- gateway/  the ONLY path to models (OpenRouter): tiers, budgets, pause switch, cost logging
        +-- brain/    the ONLY path to facts: reads, writes, embeddings, provenance
        +-- judge/    TypeSafe wrapper: gates, logging, thresholds, fail-open/closed per gate
        +-- approvals/ human review queue for irreversible actions
        +-- events/   every agent action writes an event row (drives UI glow + audit log)
supabase/migrations/  versioned SQL, RLS on every table
```

## Hard rules

- **Configurable things live in the database, not in code.** Agent prompts, agent definitions, model tiers, budgets, thresholds and knowledge sources must be changeable by the owner without editing the repo or redeploying. Do not add a hardcoded prompt, model slug or per-agent setting to code; put it in a table with versioning and an audit event. See Steps 6 and 11 in `docs/BUILD_PLAN.md`.
- **All model calls go through `api/app/gateway/`.** Never call OpenRouter or any provider directly from an agent. The gateway enforces per-agent budgets, tier routing, provider restrictions for sensitive data, the global pause switch, and logs tokens and cost per call.
- **Pause switch and kill** (ADR 023). The pause switch is a flag in Postgres (named `kill_switch` in the database) checked before every model call, run start, new task and wake-up. When on, nothing runs; paused work resumes only when the owner resumes it. The kill (`kill_everything`) turns the pause on and cancels every unfinished task, run and held action for good. Only a person can use either, never an agent.
- **Every table has `org_id` and Row Level Security from day one**, even though phase 1 has one org. Never write a table without it.
- **Every agent action emits an `events` row.** This feeds the live brain UI and the audit trail.
- **Idempotency keys on every trigger and every side-effecting action.** Serverless retries must never repeat an action.
- **Runs are short and resumable.** Function duration is limited (default 300s, Pro max 800s, 1800s beta). Long work is split into steps with LangGraph checkpoints; do not write a loop that assumes one long-lived process.
- **Per-run caps:** max steps and max tokens on every run. No unbounded loops.
- **Irreversible or external actions** (sending email or messages, spending money, changing code, publishing) go through the approval queue. Each approve or reject is stored with the agent output snapshot, because it doubles as a labeled example for judge calibration.
- **Judgments:** store raw probabilities in `judgments` (question id and version, input reference, output). Keep thresholds and policy in config, separate from raw judgments. TypeSafe confidence is not permission to act.
- **Secrets are server-side only.** Never commit keys. Maintain `.env.example`. Never expose service-role or provider keys to the browser.
- **Sensitive data:** route to no-retention / no-training providers only. Cheap loose providers are for non-sensitive work.
- **Ask before adding any paid service or dependency not listed above.**

## Conventions

- Python 3.12+, type hints everywhere, Pydantic models at boundaries, `ruff` + `pytest`. Small modules, no hidden globals.
- TypeScript strict mode. Keep the frontend thin until Step 10; do not pick the UI framework or design the brain visualization earlier.
- Migrations are additive and versioned in `supabase/migrations/`. Never edit an applied migration; add a new one.
- Tests for: gateway budget enforcement, the pause switch and the kill, idempotency, RLS isolation between two orgs, run resume from checkpoint.
- Record architectural decisions as short ADRs in `docs/adr/` (one page each).
- Commit small, one step at a time, with descriptive messages.

## Working agreement

- Work through `docs/BUILD_PLAN.md` in order. Finish a step's acceptance criteria before starting the next.
- **Stop and report at each Milestone** marked in `docs/BUILD_PLAN.md`: after Step 3 (cost per run, budget enforcement demo), after Step 5 (judge accuracy and cost), after Step 7 (agent platform demo) and after Step 8.1 (first department). Do not continue past a Milestone until the owner says go.
- When something in the plan conflicts with current docs (Vercel, Supabase, OpenRouter, LangGraph, deepagents, TypeSafe), check the live docs, tell the owner what differs, and propose a fix. Do not silently work around it.
- If a decision is listed under "Open decisions" in the plan, present 2-3 options with cost and complexity tradeoffs and recommend one. Do not decide silently.
- Never create accounts, enter credentials, or make purchases. List what the owner must do by hand.
- **Never merge or push to `main` directly.** All work reaches `main` through a pull request that the owner merges (owner, 2026-09-26).
