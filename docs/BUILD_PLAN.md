# Build Plan: Pantheon (Phase 1)

Work in order. Each step lists what to build and the acceptance criteria ("done when"). Sizes: S under a day, M a few days, L about a week. Read `CLAUDE.md` first for the rules that apply to every step.

## Owner prerequisites (done by hand; Claude Code must not do these)

- [ ] Vercel Pro team and project(s) created
- [ ] Supabase Pro project created (note the region; keep it close to the Vercel function region)
- [ ] OpenRouter account with credit and a management/provisioning key if per-agent keys are supported
- [ ] TypeSafe API key
- [ ] LangSmith or Langfuse account (free tier) for tracing
- [ ] All secrets placed in Vercel env vars and a local `.env` (never committed)

## Step 0: Foundations (S)

Build:
- Monorepo: `web/` (Next.js, TypeScript strict), `api/` (Python FastAPI, deployable as Vercel Functions), `supabase/migrations/`, `docs/adr/`.
- Tooling: `ruff`, `pytest`, `.env.example`, CI that runs lint and tests.
- Supabase Auth for a single owner login; a protected health-check route on the API.
- Verify the current recommended way to deploy a Python backend alongside a Next.js frontend on Vercel (monorepo layout, Python runtime version, bundle limits). Write ADR 001 with the chosen layout.

Done when: both apps deploy from the repo, the owner can log in, and the API health route returns 200 behind auth.

## Step 1: Brain schema v0 (M)

Build migrations for the following (a suggested starting point; refine as needed). Every table has `org_id`, RLS, and timestamps.

- `orgs`, `org_members`
- `facts`: claim text, source, source_ref, confidence, status (active/superseded/disputed), `superseded_by`, created_by_run_id, embedding (pgvector)
- `agents`: name, role, `parent_agent_id` (for department heads and workers), model_tier, config jsonb, daily_budget_usd, enabled
- `runs`: agent_id, trigger, status, started/ended, tokens, cost_usd, `idempotency_key` (unique per org), checkpoint thread id
- `events`: run_id, agent_id, type, payload jsonb, created_at (this table feeds Realtime)
- `model_calls`: run_id, agent_id, model, provider, tokens_in/out, cost_usd, latency_ms
- `approvals`: run_id, action_type, payload, agent_output_snapshot, status, decided_at, verdict
- `judgments`: run_id, gate, question_id, question_version, input_ref, output jsonb (probabilities), created_at
- `system_flags`: kill_switch and similar global flags

Also: a thin `brain/` module (insert fact, query by similarity, mark superseded) and an embedding step through the gateway.

Done when: migrations apply cleanly from scratch; a test proves org A cannot read org B's rows through RLS; you can insert a fact and retrieve it by semantic search.

## Step 2: Model gateway (M)

Build `api/app/gateway/`, the only path to models.

- OpenRouter client with tier config in one place (cheap / standard / frontier mapped to model IDs; easy to change without touching agent code).
- Per-agent budget enforcement (daily limit from `agents.daily_budget_usd`; prefer OpenRouter per-key limits if supported, else enforce with a budget check against `model_calls` before each call). Verify current OpenRouter docs for per-key limits and provider preference options.
- Provider routing restrictions for sensitive work (no-retention / no-training providers). Verify the exact request parameters in the current docs.
- Kill-switch check before every call.
- Log every call to `model_calls`; emit an event.
- Structured errors when a budget or kill switch blocks a call.

Done when: tests show a call is blocked when the agent is over budget or the kill switch is on; cost and tokens are recorded per call; switching an agent's tier changes the model without code changes.

## Step 3: First agent end to end (L)

Build one LangGraph or deepagents agent that reads from and writes to the brain through `brain/` and calls models only through `gateway/`.

- LangGraph Postgres checkpointer against Supabase. Test the pooler early: transaction-mode pooling can break prepared statements; if it does, use the appropriate connection mode and document it in an ADR.
- Runs are short and resumable; per-run max steps and max tokens.
- Tracing enabled (LangSmith or Langfuse; see Open decisions).
- Run lifecycle (`runs` row, events, cost rollup).

Done when: a run can be killed mid-way and resumed from its checkpoint; the run's total cost and token use are visible; hitting the step or token cap stops the run cleanly.

### Milestone (stop here and report to the owner)

Show: cost per run for the first agent, a budget-block demo, a kill-switch demo, and the projected monthly cost at a few run volumes. The owner sets the daily budget before anything else is built.

## Step 4: Triggers and safety (M)

Build:
- One trigger mechanism (see Open decisions) that wakes agents on a schedule or event and calls an authenticated API endpoint.
- Idempotency keys enforced by the unique constraint on `runs`, and on every side-effecting action.
- Retry behavior that is safe under serverless re-delivery.

Done when: sending the same trigger twice produces one run and one side effect; a failed run can be retried without duplicating actions.

## Step 5: TypeSafe judge (M)

Build `api/app/judge/`.

- Read the live docs first (https://docs.typesafe.ai/llms.txt) and the relevant primitive and cookbook pages. Use Choice, Noul, and Score appropriately; ask one narrow question per judgment and include the evidence in the state.
- First gate: brain write gate. Questions such as "is this claim supported by this source?" and "does it contradict an existing fact?"
- Log every judgment in `judgments`; keep thresholds and policy in config.
- Configurable fail-open or fail-closed per gate (default fail-closed for brain writes and irreversible actions).
- Cascade for uncertain cases: escalate to a frontier model with reasoning, then to the approval queue.
- Small eval harness: labeled cases (start from approval verdicts) to check thresholds; report precision and recall per gate.

Done when: a new fact is checked before insertion; judgments are logged and queryable; the service being down triggers the configured failure mode; the eval harness runs on a small labeled set.

## Step 6: Approval queue and first HOD (L)

Build:
- Approval queue for irreversible actions (create, list, approve, reject, snapshot the agent output).
- One department-head agent that delegates to one or two worker agents (deepagents subagents or LangGraph supervisor; choose and record in an ADR).
- Judge-based triage: auto-pass high-confidence low-risk items, escalate uncertain ones.

Done when: an agent action is held for approval, and approve or reject resumes or cancels it; the HOD delegates a task to a worker and the full trail appears in `events`.

## Step 7: Realtime and UI (L)

Choose the UI framework now (not before): present 2-3 options with tradeoffs.

Build:
- Supabase Realtime subscription on `events` (RLS-aware).
- A chat window to talk to the HOD and give orders in real time.
- The brain visualization at the center: it should glow and flow with information as agents work, driven by real events (not fake animation). Design after the framework is chosen.
- Model management: change the model behind each tier, and per department, without a redeploy. Picked from OpenRouter's live catalogue rather than typed free-hand, and every change audited to `events`. See `docs/adr/0003-model-selection-is-runtime-configuration.md`.

Done when: chatting with the HOD works end to end and agent activity visibly lights up the brain in near real time.

## Step 8: Cost review (S)

Build a small dashboard or query set: cost per agent per day, budget alerts, and a report of tasks that could move to a cheaper tier (using judge scores comparing cheap vs frontier outputs).

Done when: the owner can see real cost per agent per day and has budget alerts configured.

## Open decisions (present options and recommend; do not decide silently)

1. **Trigger mechanism:** Vercel Cron, Supabase `pg_cron` plus database webhooks, or a queue.
2. **Tracing:** LangSmith vs Langfuse.
3. **Per-agent spend control:** OpenRouter per-key limits vs application-level budget checks (depends on what the current API supports).
4. **Checkpointer connection mode** through the Supabase pooler (result of the Step 3 test).
5. **HOD pattern:** deepagents subagents vs LangGraph supervisor.
6. **UI framework:** decided at Step 7.

## Explicitly out of scope for phase 1

Onboarding flows for other companies, code-execution sandboxes, session-based reusable agents (phase 2), local models, self-hosted infrastructure. The `org_id` + RLS groundwork is the only multi-tenant work to do now.
