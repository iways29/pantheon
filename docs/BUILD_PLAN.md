# Build Plan: Pantheon (Phase 1)

Work in order. Each step lists what to build and the acceptance criteria ("done when"). Sizes: S under a day, M a few days, L about a week. Read `CLAUDE.md` first for the rules that apply to every step. Supporting documents: `docs/research/typesafe-jev.md` (what Jev is and how we use it) and `docs/design/agent-organization.md` (departments, agents, tools, tasks).

## Owner prerequisites (done by hand; Claude Code must not do these)

- [ ] Vercel Pro team and project(s) created
- [ ] Supabase Pro project created (note the region; keep it close to the Vercel function region)
- [ ] OpenRouter account with credit and a management/provisioning key if per-agent keys are supported
- [ ] TypeSafe API key (`TYPESAFE_API_KEY` is currently empty in `.env`; needed to run Step 5 live; create at https://console.typesafe.ai/keys and set it in `.env` and the Vercel `pantheon-api` project)
- [ ] Ask TypeSafe (privacy@typesafe.ai) how long request content is retained; needed before any sensitive data is sent to it (open decision 11)
- [x] Business described and channels chosen (Reddit, Instagram, X, newsletter, site blog; the owner owns the accounts). Phase 1 posting is by hand, so no accounts or keys are needed by agents yet; any later automation needs accounts created by the owner when that step starts
- [ ] Langfuse Cloud account (free Hobby tier) and a project for tracing; see ADR 004
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
- Tracing enabled with Langfuse (see `docs/adr/0004-tracing-with-langfuse.md`).
- Run lifecycle (`runs` row, events, cost rollup).

Done when: a run can be killed mid-way and resumed from its checkpoint; the run's total cost and token use are visible; hitting the step or token cap stops the run cleanly.

### Milestone (stop here and report to the owner)

Show: cost per run for the first agent, a budget-block demo, a kill-switch demo, and the projected monthly cost at a few run volumes. The owner sets the daily budget before anything else is built.

**Reported 2026-09-21** (evidence in ADR 006): research agent on the cheap tier, $0.000226 mean per run over 8 real runs; about $45/month of platform cost plus $0.68/month in models at 100 runs/day. **Owner decision: the `research` department's daily budget is $0.25.**

## Step 4: Triggers and safety (M)

Build:
- One trigger mechanism (see Open decisions) that wakes agents on a schedule or event and calls an authenticated API endpoint.
- Idempotency keys enforced by the unique constraint on `runs`, and on every side-effecting action.
- Retry behavior that is safe under serverless re-delivery.

Done when: sending the same trigger twice produces one run and one side effect; a failed run can be retried without duplicating actions.

Owner requirement, built into this step: **a fixed morning routine, not an all-day loop.** Triggers are rows the owner edits (agent, task, time, weekdays, time zone); they fire at most once per local day, start switched off, and never run past the kill switch, a disabled agent or department, or the department's budget. The owner can step in at any time. See ADR 008.

**Status:** code, migrations and tests done (ADR 008). Going live needs the owner's steps listed in the ADR (apply migrations, set `TRIGGER_SECRET`, two Vault secrets, deploy).

## Where the plan goes from here (restructured 2026-09-25)

Steps 0 to 4 are done. What was one "judge" step and one "first HOD" step is now
two large parts, in this order:

- **Part 1, Step 5: TypeSafe Jev as the decision layer** (fact checking, content
  screening, guardrails; later, routing, tool-risk gating, verification). Research
  in `docs/research/typesafe-jev.md`.
- **Part 2, Steps 7 and 8: the agent organization** (platform, then departments,
  heads, workers, sub-agents, tools and tasks). Design in
  `docs/design/agent-organization.md`.
- **Step 6** (configuration backend) sits between them because departments need
  knowledge, and knowledge needs the Step 5 gates.

| Old number | Now |
| --- | --- |
| 5 (judge) | 5 (gates 5.1 to 5.4), plus 7.5 (tool-risk gate), 8.2 (routing) and 9 (cascade, re-ranking) |
| 5b (configuration backend) | 6 |
| 6 (approval queue, first HOD) | 7.5 (approvals) and 7.4 and 8 (heads and workers) |
| 7 (realtime and UI) | 10 |
| 7b (Control Center) | 11 |
| 8 (cost review) | 12 |

**Milestones.** Stop and report to the owner after Step 5, after Step 7 and after
Step 8.1, in the same way as the Step 3 Milestone. Each lists what to show.

## Step 5: TypeSafe Jev, the decision layer (L, four parts)

**What Jev is and is not.** A model that returns typed answers with
probabilities instead of text. It is cheap ($0.042 per million input tokens,
output free), fast (about 100 ms) and can ask many questions of one state in one
call. It cannot write, cannot do arithmetic or date maths, gives no explanation,
and can be steered by adversarial text in the data it judges. So code owns every
workflow and Jev makes narrow judgments inside it. Details, numbers and sources:
`docs/research/typesafe-jev.md`. Read the live docs
(https://docs.typesafe.ai/llms.txt) again before coding; never guess API details.

**Rules for every gate:**
- One narrow, atomic question per judgment; the evidence goes in the state; ask
  independent questions together in one request.
- Numbers, dates and counting stay in code. Filter the state in code first.
- Pin the model version (`jev-1.13.0`), log the version on every judgment, move
  only after the calibration set passes (5.4).
- Raw probabilities go in `judgments`; thresholds and policy live in versioned
  config, separate. Confidence is a signal, never permission to act.
- Every gate has an "uncertain, ask a person" band and a fail-open or fail-closed
  setting (closed by default for brain writes and irreversible actions).
- Nothing marked sensitive is sent to TypeSafe. Its published terms promise no
  training on inputs but state no retention period, and zero retention is
  enterprise-only. Open decision 11.

**The fronts Jev covers, and where each lands:**

| Front | Step |
| --- | --- |
| Brain fact check, duplicates, contradictions, expiry | 5.2 |
| Prompt-injection and sensitive-data screening of scraped and uploaded content | 5.3 |
| Input and output guardrails (`strict` and `normal` policies) | 5.3 |
| Tool-call risk check before an action runs; approval triage | 7.5 |
| Tool and worker selection | 7.2 and 9 |
| Intake routing (which department, which model tier, or a person) | 8.2 |
| Verify-and-escalate: is a cheap-tier answer good enough? | 9 |
| Re-ranking recalled facts before an agent reads them | 9 |
| Output quality scoring, and cheap-versus-frontier comparison for cost review | 9 and 12 |

### Step 5.1: Judge core (M)

Build `api/app/judge/`:
- `JevTransport`: a thin `httpx` client for `POST /v1/systemone` (same style as
  `gateway/transport.py`; no new dependency, open decision 10). Model pinned in
  config. Typed request and response models. Retry with backoff on 429 and 5xx
  honouring `retry-after`; timeouts; a circuit breaker so an outage is not
  hammered.
- **Gates and questions as versioned data**, audited like prompts (ADR 007):
  `judge_questions` (gate, key, version, type, instructions, criteria, active)
  and `judge_gates` (gate, question-set version, thresholds, fail mode,
  `allow_sensitive` default false, enabled). Changing wording or a threshold
  takes effect on the next call with no deploy, and every change is an event.
- `judge.run(gate, state)`: sends the gate's questions in one request, writes the
  raw answers to `judgments` (question id and version, input reference, model
  version, tokens, latency), applies the gate's thresholds, and returns a
  decision with reasons.
- **Shares the gateway's protections:** kill switch checked first, cost written
  to `model_calls` (provider `typesafe`), a `judgment_made` event, counted in the
  department's spend.
- Failure handling per gate: service down, timeout, 429 or a malformed answer
  triggers the configured mode.

Done when: a gate can be defined in the database and called; the raw answer,
version and cost are recorded for every call; changing a threshold changes the
decision with no deploy; the kill switch stops judgments; TypeSafe being
unreachable produces each gate's configured failure mode; a state marked
sensitive is refused. Tests use a scripted transport, plus one live smoke test
that the owner runs once `TYPESAFE_API_KEY` is set.

**Status (2026-09-25):** code, migration and tests done (ADR 009). Live smoke
test `python -m scripts.jev_smoke` waits for the owner's run; the migration
is not yet applied to Supabase.

### Step 5.2: Brain write gate (M)

The recipe combines TypeSafe's citation-check and entity-alignment cookbooks.
Every fact write passes it; `brain.insert_fact` has no bypass.

1. **Code first, no model:** non-empty and length limits; exact duplicate by
   normalised text; provenance present; when the source text is available, the
   quote must appear in it (a missing quote is `fabricated` and rejected without
   a model call); numbers and dates are extracted and compared in code.
2. **One Jev request per claim** with a battery: Nouls for "is this a standalone
   factual claim", "is it opinion or hedged", "does it contain a secret or personal
   data", "does it contain an instruction aimed at an AI", "is it likely to
   change"; and a Choice for how the source relates to the claim (`supports`,
   `contradicts`, `says nothing`).
3. **Neighbours:** fetch the closest existing facts by vector search and ask a
   three-level Score per neighbour (different, related and possibly the same,
   same) plus a Noul "does it contradict".
4. **Decision in code** from the gate's thresholds: accept; accept as disputed;
   supersede (old fact marked `superseded_by`); skip as duplicate; reject; or
   send to review (an approval, Step 7.5). The uncertain band always goes to
   review.
5. `facts` records the judgment that admitted it, a review-after date for
   facts likely to change, and a **visibility tier** (`public` or `internal`) so
   public content can only use facts cleared for it (an idea from the owner's
   earlier `unreal-lab-os` sketch). The research agent's store step moves onto this path.

Done when: a new fact is checked before insertion; a duplicate is skipped; a
contradiction becomes disputed or goes to review; a fabricated quote is rejected
without a model call; the judgment behind every fact is queryable; and with the
service down, brain writes fail closed.

**Status (2026-09-25):** done (ADR 010). The research agent's store step uses
it. Live: apply the migrations, then run `scripts.agent seed` once to publish
the brain gates.

### Step 5.3: Content screening and guardrails (M)

Needed before Step 6 fetches web pages or reads uploads.
- **Screening** each chunk of untrusted text (scraped pages, uploaded documents,
  inbound messages) with Nouls for prompt injection (checked first), relevance,
  sensitive data, and contradiction of what is already known. Labels: `clean`,
  `quarantined`, `review`. Quarantined text never reaches an agent's context.
- **Defence in depth.** Jev can be steered by adversarial text, so screening is
  one layer. Untrusted text is always handed to models as quoted data, in tools
  with no side effects, and never as instructions.
- **Guardrails:** an input battery and an output battery (hazard Nouls plus a
  severity Score), with named policies `strict` and `normal` held as thresholds
  in config, mapped to pass, review or block.
- Tests use real prompt-injection samples, including one planted in an otherwise
  useful page.

Done when: a page carrying a planted injection is quarantined and a normal page is
`clean`; the same assessment routes differently under `strict` and `normal` with
no new model call; every screening is a judgment row.

**Status (2026-09-25):** done (ADR 011), tested with a scripted TypeSafe.
How well Jev itself labels the injection samples is measured live in 5.4.

### Step 5.4: Calibration and the labelled set (M)

- A `judge_cases` set per gate (state, expected outcome, where the label came
  from). Sources: the owner's approve and reject decisions (Step 7.5), a frontier
  model ensemble through the gateway (cost-capped), and the owner's spot checks.
- `scripts.judge_eval`: precision and recall per gate at each threshold,
  confidence against accuracy, self-consistency over repeated runs, and
  regression cases for the known weak spots (arithmetic, dates, double negatives,
  adversarial text).
- Recommended thresholds from data, not from cookbook numbers.
- The model-upgrade rule: pin, re-run the set, write the result in an ADR, then move.
- Measured cost per 1,000 judgments and latency.

Done when: the harness runs on labelled cases for the brain gate and the screening
gate and prints precision and recall; thresholds in config are set from it.

### Milestone after Step 5 (stop and report)

Show: per-gate precision and recall, cost per 1,000 judgments, latency, what fails
and why, the retention answer from TypeSafe, and the recommended thresholds.

## Step 6: Configuration backend (M)

Formerly Step 5b. Principle: **configurable things live in the database, never in
code.** The owner must not have to edit the repo or redeploy to change a prompt,
add an agent, upload knowledge or change a model. Model tiers already work this
way (ADR 003); this step brings the rest to the same standard. Backend only; the
screens come in Step 11. Write an ADR for it.

Build:
- **Prompts in the database.** Done ahead of Step 4 (ADR 007): versioned
  `agent_prompts`, per-run pinning, audit events, rollback, and a CLI.
- **Agent creation API.** Create an agent from a structured payload (department,
  name, role, tier, daily budget, starting prompt, allowed tools). New agents start
  `enabled = false` and emit an event. Extended in Step 7.4 with role type, runner
  and autonomy level.
- **Documents.** A `documents` table (source material, kept separate from
  `facts`) and a `document_chunks` table with embeddings via the gateway. Files go
  in Supabase Storage. Every document and chunk carries a `scope` (`company`,
  `department` or `agent`) plus an owner id, enforced by RLS so an agent only
  retrieves its own scope, its department's and the company's. **Every upload is
  screened (Step 5.3) before it is chunked.** A fact created from a document links
  back to it.
- **Link scraping, two steps.** (1) Fetch and preview: fetch a URL (https only,
  private and internal addresses refused), extract the text, **screen it (5.3)**,
  show the facts it would create, save nothing to the brain. (2) Push to brain: a
  separate, explicit action; each fact passes the write gate (5.2) and carries the
  URL as `source_ref`. Start with plain HTTP fetch and text extraction (free); a
  paid scraping service needs owner approval first.

Done when: changing an agent's prompt takes effect on its next run with no code
change or redeploy, and the run records which version it used; an agent can be
created through the API and starts disabled; a company doc, a department doc and an
agent doc are each retrievable only by the right agents (RLS test with two orgs and
two scopes); a URL can be previewed without touching the brain and then pushed as a
separate action; a page with a planted injection never reaches the preview as clean.

## Step 7: The agent platform (L, six parts)

Formerly the first half of Step 6, expanded. Design and reasoning:
`docs/design/agent-organization.md`. The key decision: the hierarchy (Chief of
Staff, heads, workers) is built from **durable database tasks**, not nested
in-process calls, because deepagents supports only one level of subagent, runs it
inside the parent's process, and our runs are short and resumable on serverless.
Deepagents is used for a worker's temporary helpers. Open decision 5.

### Step 7.1: Gateway tool calling (M)

The gateway today is text in, text out, and the Step 3 ADR noted agents with tools
need more. Verified against OpenRouter's current tool-calling docs (`tools`,
`tool_choice`, `parallel_tool_calls`, `finish_reason: tool_calls`, and the `tools`
parameter must be resent on every turn).
- `complete()` accepts `tools` and `tool_choice`, returns tool calls, and accepts
  `tool` role messages; routing requires providers that support the parameters.
- Check that each tier's model supports tools against OpenRouter's catalogue.
- A small LangChain chat-model adapter over the gateway, so deepagents and
  LangGraph reach models **only** through it (budgets, kill switch, cost logging,
  tracing all still apply).

Done when: a tool-using loop is blocked mid-way when the department is over budget
or the kill switch is on; cost and tokens are recorded for each call in the loop.

### Step 7.2: Tool registry and runtime (M)

- `tools` (name, description, risk class R0 to R5, approval policy, enabled) and
  `agent_tools` (who may call what), managed as data.
- Code implements the tools. The runtime wraps every call: allowlist check,
  argument validation, timeout, output size limit, an idempotency key for side
  effects, an `events` row, and Step 5.3 screening of any untrusted output.
- Starting tools: `brain_search`, `brain_propose_fact` (through 5.2),
  `read_document` (scoped), `web_fetch_preview` (screened), `create_task`,
  `report_result`.
- Where an agent has many tools, a Jev Choice picks the likely one and re-checks
  the top few.

Done when: an agent can only call tools it is allowed; a side-effecting tool called
twice with one key acts once; an injection inside a fetched page is quarantined
before the agent sees it.

### Step 7.3: Tasks and delegation (L)

- `tasks` table and lifecycle (`queued`, `running`, `blocked`, `awaiting_approval`,
  `done`, `failed`, `cancelled`), parent and child links, budgets per task, and
  idempotency keys. Runs (already built) advance tasks.
- The Step 4 scheduler pokes runnable tasks. A trigger now creates a task from a
  template. A parent blocks on its children and resumes when the last finishes.
- **Limits enforced in the database:** tree depth 3, at most 4 children per task,
  at most 50 tasks per department per day, a child's budget within the parent's
  remaining budget. Only the Chief of Staff and heads may create tasks; no agent may
  create or schedule agents or triggers. Open decision 13.

Done when: a head splits a task into two worker tasks and finishes when both do,
across separate short invocations; the limits refuse the fifth child and the fourth
level; a repeated order is one task.

### Step 7.4: Agent definitions and runners (L)

- `agents` gains `role_type` (`chief_of_staff`, `head`, `worker`), `runner`,
  `autonomy_level`, `max_children`. Creation and editing go through the Step 6 API.
- **Runners:** `pipeline` (a fixed LangGraph graph; default), `deep` (a deepagents
  loop with the Postgres checkpointer and the gateway adapter, for open-ended
  work), `router` (mostly Jev), `digest` (gather and summarise).
- The **head runner** plans, creates worker tasks, blocks, and on resume checks the
  results. The **worker runner** does one task and writes a short structured result.
  Temporary sub-agents exist only inside one run, for context isolation.
- Record the decision in an ADR (open decision 5).

Done when: one head and two workers complete a real task tree with the cost of every
level visible on the task, and a worker's temporary helper does not appear as a
persisted agent.

### Step 7.5: Approvals and the tool-risk gate (M)

Formerly the approval half of Step 6. The `approvals` table already exists.
- Create, list, approve, reject and edit. Each decision stores the agent's output
  snapshot.
- A held action sets its task `awaiting_approval`. Approve resumes it from its
  checkpoint (deepagents `interrupt_on` with `Command(resume=...)` on the same
  thread), reject cancels or redirects it.
- **Tool-risk gate (Jev):** before a call runs, questions about reversibility,
  external effect, spending money, whether it matches the task's intent, and
  sensitive content. R4 calls are always held; below R4, high-confidence low-risk
  calls pass, and the uncertain band goes to a person. Thresholds scale with risk.
- Every decision is stored as a labelled example for Step 5.4.

Done when: an R4 action is held and approve or reject resumes or cancels it; a
low-risk call auto-passes only above its threshold; the full trail is in `events`.

### Step 7.6: Autonomy and safety limits (S)

- The autonomy ladder per agent (L0 draft only, L1 default, L2, L3), set only by
  the owner; the system can recommend a promotion from approval history but never
  makes one.
- Loop detection (the same tool with the same arguments three times), a
  stuck-task reaper, per-agent task rate limits, and the kill switch stopping
  tasks as well as runs.

Done when: each limit has a test that trips it, and a task tree stops cleanly
mid-flight when the kill switch is switched on.

### Milestone after Step 7 (stop and report)

Show: a head delegating to two workers under all limits, an approval-held action
resumed, the kill switch mid-flight, and cost per task tree.

## Step 8: Departments (L, one at a time)

Each department is built, then run **unattended on its morning routine for five
mornings** before the next starts. Start with three; the owner confirms the list.
The proposal, charters and starting budgets are in
`docs/design/agent-organization.md`, section 8. Open decision 12.

- **8.0 Charters (S).** Each department's charter is a database row the owner can
  edit: purpose, head, workers, tools, morning routine, approval rules, Jev gates,
  budget, autonomy level, data sensitivity, metrics. The owner reviews them first.
- **8.1 Research and Intelligence (M).** The existing researcher becomes a
  department: Research Lead, web researcher, fact curator. Morning routine: an
  overnight brief, and brain hygiene (stale or disputed facts). Every fact passes 5.2.
  **Milestone: stop and report** (five unattended mornings, cost, facts admitted and
  rejected, what the owner would change).
- **8.2 Executive Office and Chief of Staff (M).** Intake: a Jev Choice over the
  departments (descriptions read from the database) plus a complexity Score, giving
  the department, the model tier, or a person. An order endpoint and CLI so the
  owner can give an order at any time. The morning brief and the approvals digest.
- **8.3 Marketing and Content (L).** The owner's first revenue department (chosen
  2026-09-25). Content Lead, topic researcher, writer, editor and fact-checker,
  distribution scheduler. Drafts only; every publish is an approval. Claims in drafts
  are checked against the brain with Jev, and the owner's edits are stored as
  examples of their voice. Charter and morning routine: design doc, section 8.1; the business, voice and
  hard content rules: `docs/business/the-unreal-lab.md`. Each draft records the
  facts it relied on (`artifact_claims`) so a wrong fact can be traced to every
  piece that used it.
  Needs from the owner: what the business sells and to whom, example content in the
  voice they want, the channels, and any banned claims. Sales and Outreach follows it.
- **8.4 Product and Engineering (L).** Any change to code is an approval, always.
- **8.5 Finance and Cost controller (M).** Read-only on money; feeds Step 12.
- **8.6 Founder Relations (deferred).** Inbound founder applications read and scored
  with Jev, reply drafts for approval, honouring "we answer within a week". Built when
  applications start arriving (open decision 16).
- **8.7 Later.** Support, Legal or Compliance, LP and partner relations
  (sensitive; see the brief's rule on fund language).

Done when, for each department: its morning routine runs unattended five mornings
in a row inside its budget, no external action happened without an approval, and
the owner has stepped in at least once to give an order and had it obeyed.

## Step 9: Jev in the loop (M)

With real agents running, use Jev to lower cost and raise quality. Only start once
Step 8.1 is running.
- **Verify and escalate (cascade):** a cheap-tier answer is checked by Jev with
  narrow "is this wrong?" questions per field; any flag re-runs the task on a
  stronger tier.
- **Re-rank recalled facts** before an agent reads them (relevance, contradicts the
  question's premise, stale).
- **Run and output scoring** for quality tracking, and cheap-versus-frontier
  comparisons for Step 12.
- Measure against always using the standard tier.

Done when: on the labelled set the cascade costs less per task than always-standard
with no drop in quality, and the numbers are in a report.

## Step 10: Realtime and UI (L)

Formerly Step 7. Choose the UI framework now (not before): present 2-3 options with
tradeoffs.

Build:
- Supabase Realtime subscription on `events` (RLS-aware).
- A chat window to talk to the Chief of Staff and heads and give orders in real time
  (the order endpoint from 8.2 is its backend).
- The brain visualization at the center: it should glow and flow with information as
  agents work, driven by real events (not fake animation). Design after the framework
  is chosen.
- Model management UI (inside the Control Center, Step 11): change the model behind
  each tier, and per department, without a redeploy. The backend (table, gateway
  lookup, catalogue check, audit trigger) already exists; this is the screen on top
  of it. Picked from OpenRouter's live catalogue, and every change audited to
  `events`. See `docs/adr/0003-model-selection-is-runtime-configuration.md`.

Done when: chatting with the Chief of Staff works end to end and agent activity
visibly lights up the brain in near real time.

## Step 11: Control Center tab (M)

Formerly Step 7b. A new tab in the web app: the owner's place to change everything
configurable, with no code edits. Screens over the Step 6 backend and the Step 7 and
8 tables:

1. **Agents and departments.** A common intake form per department (department,
   name, role type, runner, tier, budget, starting prompt, tools, autonomy level).
   Creates the agent disabled; the owner reviews and enables it. Department charters.
2. **Edit prompts.** Edit an agent's prompt, see version history, roll back.
3. **Knowledge.** Upload documents with a scope picker (company, department or
   agent), and see what each agent can read.
4. **Add links.** Paste a URL, preview the screened extract, then an optional "Push to
   brain" button.
5. **Model management.** The tier and per-department model screen from Step 10.
6. **Morning routine.** Add, edit, enable and disable the scheduled tasks: what each
   agent does each morning, at what time, on which days. Shows what fired and what it
   cost.
7. **Tools and approvals.** Which agent may use which tool, the approval queue, and
   the autonomy ladder.
8. **Judge.** Gates, question wording, thresholds and policies, with the calibration
   report beside them.
9. **Tasks.** Every task tree with status, cost and who created it.
10. **More to come.** The owner will add further features; the tab is a list of
    sections so a new one is a new section, not a redesign.

Done when: the owner can do items 1 to 6 entirely from the browser, every change is
audited to `events`, and none of them needed a code change or redeploy.

## Step 12: Cost review (S)

Build a small dashboard or query set: cost per agent, per department and per task
tree per day, judge cost, budget alerts, and a report of tasks that could move to a
cheaper tier (using the Step 9 comparisons of cheap and frontier outputs).

Done when: the owner can see real cost per agent per day and has budget alerts
configured.

## Open decisions (present options and recommend; do not decide silently)

1. **Trigger mechanism:** ~~Vercel Cron, pg_cron plus webhooks, or a queue.~~ Decided: Supabase `pg_cron` + `pg_net`, with schedules stored in a table. See ADR 008.
2. **Tracing:** ~~LangSmith vs Langfuse.~~ Decided: Langfuse Cloud, free Hobby tier. See ADR 004.
3. **Per-agent spend control:** ~~OpenRouter per-key limits vs application-level checks.~~ Decided: departments hold the budget, enforced in the application. See ADR 002.
4. **Checkpointer connection mode** through the Supabase pooler. ~~Open.~~ Decided: transaction pooler, checkpoints in a private `langgraph` schema. See ADR 005; verified against the live pooler.
5. **How the hierarchy is built.** Options: (A) deepagents subagents only, which is one level, in-process and blocking, so it cannot express Chief of Staff, heads and workers; (B) a LangGraph supervisor, also in-process, which holds a run open while children work and fits serverless badly; (C) **durable database tasks for the tree, deepagents only for a worker's temporary helpers** (recommended: survives serverless limits, every step auditable, budgets and limits enforceable in SQL, resumable). Cost of C: a tasks table and scheduler extension to build (Step 7.3). Record in an ADR at 7.4.
6. **UI framework:** decided at Step 10.
7. **Document scopes:** company, department and agent (recommended) vs company and agent only. Owner leaned toward the recommendation; confirm at Step 6.
8. **When to do prompts-in-database:** ~~now vs with the rest.~~ Decided and done ahead of Step 4. See ADR 007.
9. **Scraping:** plain HTTP fetch (free, no JavaScript rendering) vs a paid scraping service (needs owner approval).
10. **How to call TypeSafe.** **Decided (owner, 2026-09-25): (A), see ADR 009.** (A) **Our own thin `httpx` transport to `api.typesafe.ai`** (recommended: no new dependency, matches the gateway's style, keeps kill switch, cost logging and events in one place; uses the `TYPESAFE_API_KEY` already in the plan). (B) The official `typesafe-sdk` package (retries and typed responses built in; a new dependency to approve). (C) Through OpenRouter as `~typesafe/jev-latest` (one key and one bill, but the alias moves, which ADR 003 forbids; a versioned id would be needed). (D) Through Vercel's AI Gateway (another moving part). Do not adopt `langchain-typesafe` (experimental, new dependency); copy its patterns.
11. **Sensitive data and TypeSafe.** **Decided (owner, 2026-09-25): not allowed; a per-gate switch, off by default. See ADR 009.** Recommended: **not allowed** until TypeSafe confirms request retention in writing (their terms are silent on it and zero retention is enterprise-only). Sensitive items go to a person or to a zero-retention LLM judge through the gateway. The owner may email privacy@typesafe.ai; the answer is reported at the Step 5 Milestone.
12. **Which departments first.** Decided in part: Executive Office, Research and Intelligence, then **Marketing and Content** as the first revenue department (owner, 2026-09-25); Sales and Outreach follows. The business, channels (Reddit, Instagram, X, newsletter, site blog every two weeks or as needed) and voice are in `docs/business/the-unreal-lab.md`. Still owed: sample content in the right voice, once there is any.
13. **Delegation limits.** Recommended defaults: tree depth 3, four children per task, 50 tasks per department per day. The owner can set others; enforced in the database.
14. **Where labels come from** for calibration: the owner's approval decisions (best, slow to accumulate), a frontier-model ensemble (fast, costs a few cents, needs spot checks), and the owner's spot checks. Recommended: start with the ensemble, verify a sample by hand, add approvals as they accumulate.
15. **Entity and relationship layer.** The owner's earlier `unreal-lab-os` sketch models companies, people and investors with `FOUNDED`, `INVESTED_IN`, `ADVISES` and `COMPETES_WITH` edges and warm-introduction paths. Our brain holds facts only. ~~Now or later?~~ **Decided: later** (owner, 2026-09-25). When it is time: `entities` and `relationships` tables in **Postgres** (not the graph database, which is outside the locked stack); short paths are recursive queries.
16. **How Pantheon relates to The Unreal Lab.** ~~Open.~~ **Decided (owner, 2026-09-25): Pantheon is for the owner to operate The Unreal Lab.** Expanding to onboarded companies is a possible future and not fixed, so only the multi-tenant groundwork is built. **Founder Relations is deferred** (nothing arrives yet; applications come by email).

## Explicitly out of scope for phase 1

Onboarding flows for other companies, code-execution sandboxes, session-based reusable agents (phase 2), local models, self-hosted infrastructure, agents creating or scheduling other agents, paid scraping services, and any tool that moves money or handles credentials. The `org_id` + RLS groundwork is the only multi-tenant work to do now.
