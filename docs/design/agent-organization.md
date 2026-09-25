# Design: the agent organization

Status: **proposal for the owner to review** (2026-09-25). Written for Steps 7
and 8 of `docs/BUILD_PLAN.md`. Nothing here is built. Where a choice is the
owner's, it is marked **Owner** and repeated in the plan's open decisions.

The departments in section 8 are a starting proposal. They assume Pantheon is
a company run mostly by agents and not yet earning revenue (the cost target in
`CLAUDE.md` says so). The owner has not yet described the business, so the
department list must be confirmed or replaced before Step 8.

## 1. What we are designing for

1. **The owner is in charge.** Agents do a fixed routine each morning
   (Step 4), and otherwise wait. The owner can give an order, stop everything,
   or change any agent's behaviour at any time, with no code change.
2. **Cost first.** Most work is cheap-tier and rule-driven. Expensive models
   are an escalation, chosen by a Jev check, not a default.
3. **Auditable.** Every delegation, tool call, judgment and approval is a row
   the owner can read.
4. **Serverless-shaped.** Nothing waits in a long-lived process. Work is durable
   rows plus short resumable runs (ADR 006).
5. **Nothing irreversible without approval.** Sending, spending, publishing and
   changing code go through the approval queue.

## 2. The organization

```
Owner
  |
Chief of Staff  (one agent, Executive Office)         intake, routing, daily brief
  |
Department Head (HOD, one per department)              plans the department's work
  |
Worker          (specialists, each with own tools)     does a task, returns a result
  |
Sub-agent       (ephemeral helper INSIDE one run)      isolates a noisy subtask
```

- **Chief of Staff (CoS):** the front door. Takes the owner's orders and
  inbound requests, decides which department gets them (a Jev routing
  judgment, section 6), assembles the morning brief and the approvals digest.
  It does no department work itself.
- **Head of Department (HOD):** owns a department's goals, plans a task into
  smaller tasks, hands them to workers, checks the results, reports up. Holds
  no tools that touch the outside world.
- **Worker:** a specialist. Has a narrow tool set, a model tier chosen for its
  job, and its own prompts. Produces one result per task.
- **Sub-agent:** not a durable agent. A worker or HOD may spin up a temporary
  helper *within one run* so that a noisy subtask (fifty search results) does
  not fill its context. It exists only for that run.

**Three durable levels, one temporary.** Deepagents, the library we planned
to use, only supports one level of subagent below an agent, and a subagent
"cannot create its own subagents". Its subagents also run inside the
parent's process and block it. That does not fit a tree of departments on a
serverless host. So the tree above is built from **database tasks**, and
deepagents is used only for the last hop, a worker's temporary helpers.

## 3. Work orders: the backbone

A **task** is a unit of work someone asked for. It is a database row that
outlives any process. A **run** (already built) is one attempt to advance a
task, resumable from its checkpoint (ADR 006).

`tasks` (sketch; every table has `org_id`, RLS and timestamps):

| Column | Meaning |
| --- | --- |
| `id`, `parent_task_id`, `root_task_id`, `depth` | the tree; `depth` is capped |
| `department_id`, `assigned_agent_id` | who is responsible |
| `created_by` | `owner`, `trigger`, `agent:<id>` |
| `title`, `instructions`, `input jsonb` | what to do |
| `status` | `queued`, `running`, `blocked`, `awaiting_approval`, `done`, `failed`, `cancelled` |
| `blocked_on` | child tasks or an approval |
| `max_tokens`, `max_cost_usd` | this task's own ceiling |
| `result jsonb`, `artifact_refs` | a short structured summary, and pointers to files |
| `idempotency_key` | unique per org; a repeated order is one task |
| `priority`, `due_at` | ordering |

How work flows:

1. An **order** arrives: the owner's request, a morning trigger (Step 4
   becomes "a trigger creates a task from a template"), or a worker
   request. It becomes a `queued` task.
2. The scheduler pokes the API to advance runnable tasks (the mechanism from
   ADR 008, extended from runs to tasks).
3. A **HOD run** reads its task, plans, and creates child tasks for workers,
   then sets itself `blocked` on them and **stops**. Nothing waits.
4. Each **worker run** does its task with its tools and writes a `result`.
5. When the last child finishes, a database rule makes the parent runnable
   again. The HOD run resumes from its checkpoint, checks the results, and
   completes, or creates follow-up tasks.
6. Anything that needs the owner (an approval) sets the task
   `awaiting_approval`. Approving it resumes that task, rejecting it cancels or
   redirects it.

**Delegation rules, enforced in the database as well as in code:**

- Only the CoS and HODs can create tasks. Workers cannot, which keeps the tree
  at most three deep.
- Default limits (owner can change in config): tree depth 3, at most 4 children
  per task, at most 50 tasks per department per day.
- A child's budget cannot exceed what its parent has left.
- An agent cannot create, edit or schedule other agents or triggers. Only the
  owner can. A routine cannot grow into an all-day loop.
- Creating a task is idempotent by key, so a retried run cannot double-assign.

## 4. What an agent is

An agent is a **row plus a runner** (the code that executes it). The code
defines *capabilities* (which runners and tools exist). The database says
*which agent uses which*, so the owner can create and change agents from the
Control Center without a deploy (the rule in `CLAUDE.md`).

`agents` gains (additive migration): `role_type` (`chief_of_staff`, `head`,
`worker`), `runner`, `autonomy_level`, `max_children`. Already there:
`parent_agent_id`, `model_tier`, `department_id`, `enabled`, budget sub-cap,
and versioned prompts (ADR 007).

**Runner types** (few, general, reused):

| Runner | What it is | Use for | Cost |
| --- | --- | --- | --- |
| `pipeline` | a fixed LangGraph graph, like the research agent today | repeatable work with known steps | lowest, predictable |
| `deep` | a deepagents loop: plans, calls tools, may use temporary helpers | open-ended work that needs a tool loop | higher; capped by steps and tokens |
| `router` | mostly Jev judgments, one small LLM call at most | the CoS intake; triage | very low |
| `digest` | gathers rows and writes a summary | daily brief, approvals digest | low |

Default to `pipeline`. Promote a job to `deep` only when a fixed graph cannot
express it. This is the biggest single lever on the monthly bill.

Every model call goes through the gateway (Step 7.1 teaches it tool calling).
Deepagents and LangGraph reach models only through a small adapter that wraps
the gateway, so budgets, the kill switch and cost logging still apply.

## 5. Tools

A **tool** is a typed function an agent may call. Code implements it; the
database says who may use it and how risky it is. Every call emits an
`events` row, has an argument schema, has a timeout and an output size limit,
and carries an idempotency key if it has a side effect.

Tables: `tools` (name, description, risk class, approval policy, enabled) and
`agent_tools` (which agent may call which tool).

**Risk classes:**

| Class | Meaning | Examples | Default policy |
| --- | --- | --- | --- |
| R0 | read internal data | search the brain, read a scoped document, read costs | allowed |
| R1 | write internal data | propose a fact (**through the Jev brain gate**), save a note or artifact | allowed, gated |
| R2 | read the outside world | fetch a URL | allowed; the result is **untrusted** and screened by Jev before an agent sees it |
| R3 | reversible outside effect | save an email *draft*, create a calendar hold | approval until the agent is trusted |
| R4 | irreversible outside effect | send an email or message, publish, merge or deploy code, spend money | **approval, always**, unless the owner opts a specific tool in at a high autonomy level and Jev judges it low risk |
| R5 | never built | move money, handle credentials, delete data | prohibited (`CLAUDE.md`) |

**Starting toolset (Step 7.2):** `brain_search`, `brain_propose_fact`,
`read_document`, `web_fetch_preview`, `create_task` (CoS and HODs only),
`report_result`. Email, calendar, chat, GitHub and CRM tools come with the
department that needs them (Step 8), each behind approval, and each needs an
owner-created external account and key (agents never create accounts or enter
credentials).

When an agent has many tools, a Jev Choice over the tool descriptions picks
the likely tool, then re-checks the top few (the skill-suggestion pattern).

## 6. Where Jev makes the decisions

Jev is the judge and router, not an agent. Code calls it; code decides. Each
row names a gate configured in the database (`judge_gates`, Step 5.1).

| Gate | Question shape | Outcome | Step |
| --- | --- | --- | --- |
| Brain write | Noul battery, source-support Choice, duplicate/contradiction Score | accept, supersede, dispute, reject, review | 5.2 |
| Untrusted content | injection, relevance, sensitive-data Nouls per chunk | clean, quarantine, review | 5.3 |
| Input/output guardrails | hazard Nouls plus severity Score, policy `strict` or `normal` | pass, review, block | 5.3 |
| Intake routing | Choice over the departments (descriptions read from the database) plus a complexity Score | which department, which tier, or a person | 8.2 |
| Tool-call risk | reversibility Choice, external-effect, spends-money and matches-intent Nouls | auto, ask, block | 7.5 |
| Tool/worker selection | Choice over tool or worker descriptions, then re-check | which one | 7.2, 9 |
| Verify and escalate | per-field Nouls ("is this wrong?") on a cheap-tier answer | keep, or re-run on a stronger tier | 9 |
| Recall re-ranking | relevance and contradiction Nouls per recalled fact | which facts reach the model | 9 |

All of them: raw probabilities in `judgments`, thresholds in versioned
config, an explicit "uncertain, ask a person" band, and a fail-open or
fail-closed setting per gate (closed by default for brain writes and
irreversible actions). Confidence is a signal, never permission to act.

## 7. Guardrails against "going crazy"

- Triggers start switched off, fire once per local day, respect the kill
  switch, and a missed slot is skipped after a grace period (ADR 008).
- Per-run caps (steps, tokens), per-task caps, department daily budgets
  (ADR 002), and the org kill switch, which also stops tasks.
- Delegation limits from section 3, enforced in the database.
- A loop detector: the same tool with the same arguments three times in one run
  stops the run.
- A stuck-task reaper: a task with no progress past its limit is marked
  failed and reported in the brief.
- Autonomy ladder per agent (below), so trust is earned.
- Every approve or reject stores the agent's output snapshot. That history is
  the labelled data that calibrates the Jev gates (Step 5.4).

**Autonomy ladder** (owner sets it per agent, never automatic):

| Level | Agent may do without asking |
| --- | --- |
| L0 draft only | R0 only; everything else is proposed for approval |
| L1 default | R0, R1 (gated), R2 (screened) |
| L2 | plus R3 |
| L3 | plus specific R4 tools the owner names, when Jev judges the call low risk with high confidence |

The system may *recommend* a promotion from approval history (for example 30
approvals, 95% approved unchanged). Only the owner changes a level.

## 8. Departments (proposal, needs the owner)

Every department has a **charter**, kept as data in the database and editable
in the Control Center: purpose, HOD, workers, tools, its morning routine,
approval rules, Jev gates, daily budget, autonomy level, data sensitivity, and
what "working" means (metrics). Start with three departments, not seven.

| Department | Purpose | HOD | Workers (start) | Main tools | Morning routine (fixed) | Approval needed for |
| --- | --- | --- | --- | --- | --- | --- |
| **Executive Office** | intake, the daily brief, keeping the owner in control | Chief of Staff | Brief writer, Approvals clerk | brain search, create task, read costs and approvals | compile the morning brief (overnight results, approvals waiting, yesterday's cost) | nothing external |
| **Research & Intelligence** (exists) | learn things and keep the brain accurate | Research Lead | Web researcher, Fact curator, Competitor analyst | web fetch, brain propose, read documents | overnight news on the owner's topics; competitor watch; brain hygiene (find stale or disputed facts) | nothing external |
| **Revenue: Sales & Outreach** or **Marketing & Content** (**Owner** picks first) | find and warm leads / produce content | Sales Lead / Content Lead | Lead researcher, Outreach drafter (Sales); Writer, Editor (Content) | web fetch, brain, email draft, document write | shortlist of leads or content ideas; drafts | **all** sends and publishes |
| Product & Engineering (later) | build and maintain the product | Eng Lead | Spec writer, Implementer, Reviewer, Docs writer | GitHub read, branch and PR | triage issues, review open PRs | every change to code |
| Finance & Cost (later) | watch spend, protect the budget | Controller | Cost analyst, Budget sentinel | read costs, read budgets | cost and budget alert summary | any change to a budget |
| Support, Legal/Compliance (phase 2) | customers and their rules | later | later | later | later | later |

Spend, roughly (estimates to be measured at the Step 7 milestone): a routine
run on the cheap tier cost about $0.0002 to $0.0003 in Step 3. A department
morning routine of a HOD plus three workers is on the order of ten runs, so
cents a day at the cheap tier. Start each department at $0.10 to $0.25 a day
(the research department is at $0.25); the whole starting company stays under
about $1 a day, and the owner raises limits as results justify.

## 9. Build order

Steps 5 (Jev), 6 (configuration backend) and 7 (agent platform) come first,
because a department is only as good as the gates, knowledge and tools under
it. Then departments one at a time (Step 8), each proven over several
unattended mornings before the next starts. The plan has the details.

## 10. What the owner needs to decide or provide

1. **What the business is**, in a few sentences, and who the customers are.
   Departments follow from it.
2. **Which revenue department first**: Sales & Outreach or Marketing & Content.
3. **The morning time** (time zone is America/New_York) and how much the owner
   wants to see in the brief.
4. **Which outside accounts exist** or may be created (email, a social account,
   GitHub), because every outward tool needs one, made by the owner.
5. **Approval appetite:** review every send for now (recommended), or allow
   some auto-send later.
6. **The delegation limits** in section 3, or accept the defaults.
