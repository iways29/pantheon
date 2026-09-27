# Design brief: the Control Center, where the owner configures Pantheon

What it is. The Control Center is the second tab beside the Brain. The Brain is where the owner watches and steps in. The Control Center is where the owner changes how the company works, without touching code or the database. Everything here is stored as data, keeps its history, and is logged the moment it changes. Use the same look and voice as the Brain: dark by default with a light mode, myth-coded, restrained, few words chosen well. Reuse the Brain's components wherever they fit: the approval card, the agent detail, the Pause and Kill controls, the status marks and the pulse strip. Don't redesign them.

The one feeling. A quiet, precise instrument panel. Changes are deliberate: the owner sees what a change will do before making it, and can always see what changed and undo it.

## Shared patterns (design these once, use them on every screen)

1. Versioned things. Prompts, charters, judge questions and gates keep every version. Show the live version; the history, with who changed it, when, and an optional note; a side-by-side comparison of any two versions; "make this version live" to roll back.
2. Preview before apply. When a change creates or removes things, show a plain summary first, for example: "Creates 3 agents (switched off), updates 1 prompt, switches off 1 routine." The owner confirms.
3. Created switched off. New agents, departments, routines and tools always start off. Switching on is its own clear action, the owner's go.
4. Change log. Every screen has a "history" drawer: the audit events for that thing, in plain words, for example "Research budget $0.25 → $0.50, you, 09:14". There is also one company-wide log, filterable by screen.
5. Locked rules. Some things are fixed for safety and must look fixed, with a one-line reason: R4 tools always ask; only a person can pause, kill or decide an approval; an MCP tool whose definition changed stays off until re-approved.
6. Cost next to every setting that spends money: the budget, today's spend, and the last 7 days.
7. Empty states that say what to do first, not "no data".
8. Status is never shown by colour alone. Keep the reduced-motion setting.

## Screens

1. Departments and charters.
   - List of departments: name, on/off, daily budget, spend today, head, number of agents, and whether the charter is a draft or live.
   - Charter editor: purpose (plain text); head and workers, each with name, role type (head, worker, Chief of Staff), runner (deep, pipeline, router, digest), model tier, allowed tools and prompts; the morning routine items; approval rules in plain English; which judge checks the department uses; daily budget, autonomy level, success measures, and a draft switch.
   - Actions: publish (makes a new version live; a draft can't be applied); apply (with the preview); switch on or off (all of its agents and routines together); department report: whether it met its "done" test over the last N days, with cost per run.
2. Agents (intake form and list).
   - Form: department, name, role type, runner, model tier, daily budget, starting prompt, tools, autonomy level (L0 to L3). The agent is created switched off.
   - List: filter by department, state, runner and level. Bulk switch on or off.
3. Prompts.
   - Per agent, prompts sit in named slots: `system`, `explain` (how it explains itself on an approval card), `extract` (how facts are pulled from a page), `brief` (the brief writer).
   - Editor with version history, comparison, note, and roll back.
   - Show which runs used which version.
4. Morning routine (scheduled tasks).
   - Each routine: agent, title, instructions, time, days (Monday to Sunday chips), time zone, grace minutes, max steps, max tokens, and input. Research's input is topics plus source URLs; the brief's input is its mailing list.
   - On/off switch for each routine.
   - Timeline of what fired, when, how it ended (done, failed, paused) and what it cost.
   - "Run now".
5. Knowledge.
   - Documents: upload, then pick a scope (whole company, one department, or one agent). Show the screening result (clean, or flagged with the reason), chunks and size.
   - Links: paste a URL, see the screened extract and the facts it would add, then an optional "Push to brain".
   - Research topics and sources: add and remove, with a check that each source can be read.
   - "What can this agent read?": pick an agent and see its documents, departments and sources.
6. Models and spend.
   - Tiers: cheap, standard and frontier. Each points to an OpenRouter model, company-wide, with optional overrides per department. Show each model's price per million tokens in and out.
   - Budgets: per department and per agent, with today, 7 days and 30 days against the budget, and the monthly total against the owner's ceiling (target $50–200 a month).
   - Suggested tier: the tier the Chief of Staff suggests for each order, shown beside the tier actually used.
7. Tools and MCP.
   - Built-in tools: name, description, risk class (R0 to R4, each explained in one line), approval policy, on/off, timeout, output cap, daily call cap, and tool settings (for web search: search engine, results per search, searches per day).
   - MCP servers: add a server by its address; sign in with one click; status: new, needs sign-in, connected or error, with the error in plain words; "refresh tools".
   - Tools found on a server: description, inputs, the server's own safety hints, suggested risk. The owner chooses a risk class and an optional daily cap, then approves. A tool the server changed shows as "changed, switched off" with a comparison of the old and new definitions.
   - Assign tools: a grid of agents by tools, with each cell showing whether that tool would run, get a check by Jev, or ask the owner, given the agent's autonomy level.
8. Autonomy and limits.
   - The ladder: a grid of levels L0 to L3 by risk classes R0 to R3. Each cell is run, check with Jev, or always ask. R4 is shown as always ask and locked.
   - Per agent: its level. Promotion suggestions come from approval history: for example, "writer: 32 decisions, 97% agreement with you, suggest L2". The owner accepts or ignores each one.
   - Limits: how deep work can be handed down, and sub-tasks per task; tasks per department per day, and tasks per agent per hour; the loop limit (the same tool call repeated); when a task counts as stuck (minutes); the promotion bar: minimum decisions and minimum agreement.
   - Reuse the Brain's Pause and Kill controls here too.
9. Judge (Jev).
   - Checks (gates): screening pages, checking a fact before it enters the brain, tool risk, routing orders, ranking the brief, clashes with owner decisions, and others.
   - For each gate: on/off and the model it uses; fail open or closed if Jev is unreachable; whether sensitive data is allowed (off by default); thresholds and settings as named numbers or words, for example "tier for a simple order: cheap" or "brief: lead with 5 items"; version history.
   - Questions: versioned. Each has a type (yes or no, choose one, or a score), instructions and options.
   - Calibration: how often Jev agrees with the owner's decisions, per gate and per action type, with the labelled examples behind it.
10. Standing rules. The owner's rules in plain English, for example "No research on crypto tokens". Each shows when it was added and how many orders or approvals it has flagged. Add, and retire.
11. Newsletter and email.
    - Mailing lists: name, sender, reply-to, recipients (a form to add and remove people, up to 50, with checks that each address is valid), subject with {date}, time zone, on/off, and "sends on its own / waits for approval". Include a warning when switching a list to "on its own" once it has recipients other than the owner.
    - Outbox: the last emails with status (waiting for approval, sent, failed with Resend's reason, cancelled) and "retry".

## Phone

The phone version shows the change log, routines on/off, department on/off, mailing list recipients, and the MCP re-approval list. Deep editing (charters, prompts, the ladder, the judge) is desktop only, and a clear note on the phone says so.

## Data scale

Expect up to about 10 departments, 50 agents, 100 tools and a few hundred prompt versions. Lists must stay readable at those sizes, with search and filters.

Use Liquid Glass throughout.
