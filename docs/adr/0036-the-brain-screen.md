# ADR 036: The brain screen: live events, a map of facts, reads through the API

- **Status:** Accepted (owner, 2026-09-27: "go with your recommendations")
- **Date:** 2026-09-27
- **Step:** 10 (realtime and UI); the designs are in `docs/design/pantheon-design/`

## Context

Step 10 builds the Brain tab from the owner's designs: facts as points of
light that sit near facts of the same meaning, agents around them, and motion
only when a real event happens. The backend already had the events, the
approval cards, pause and kill, and `POST /orders`. It lacked a live stream
to the browser, a place for each fact on the screen, and the reads that put
the picture together (agent states, the day's pulse, the chat, an order's
path and cost). The plan asked for a UI framework to be chosen at this step;
Next.js has been in place since Step 0.

## Decision

| Question | Decision |
| --- | --- |
| Framework | Keep **Next.js** (already deployed as `pantheon-web`) |
| What draws the brain | **PixiJS** (free, GPU, holds 10,000+ points); the lens chrome stays SVG/DOM |
| Live stream | **Supabase Realtime** on `events` (added to the `supabase_realtime` publication). RLS applies per subscriber |
| Snapshot reads | **API endpoints** (`app/screen.py`, facts through `app/brain/view.py`), so `brain/` stays the only path to facts |
| Where a fact sits | **Projection of the stored embeddings** (numpy; two principal components, k-means neighbourhoods). A new fact goes beside its nearest placed facts; a new topic goes to the rim. A placed fact never moves |
| Neighbourhood names | The librarian names up to 3 per run (one cheap call each, prompt `name_neighbourhood`, data). Until then: the graph's commonest thing, else the start of a claim |

**New tables** (migration `20260928090000_brain_screen.sql`, `org_id` and RLS):
`brain_layouts` (the projection), `brain_neighbourhoods`, `fact_positions`.
The settings are the `brain_layout` flag (neighbours averaged, the distance
that counts as a new topic, jitter, the maximum number of neighbourhoods,
names per run), audited like every flag.

**Reads** (owner only, as the owner under RLS):

| Endpoint | For |
| --- | --- |
| `GET /screen/snapshot` | Everything drawn at load: status, pulse, departments, agents with their state, neighbourhoods, facts with places, held claims |
| `GET /screen/pulse` | Today's strip: spend against budget, facts added and rejected, tasks done, what needs the owner |
| `GET /screen/status` | Running or paused, and since when |
| `GET /screen/agents` | Agents and departments alone: what an event changes, without the facts |
| `GET /screen/events?since=` | Replay; model calls, judgments and run steps are left out unless `quiet=false` |
| `GET /facts/{id}` | The fact panel: source, who found it, Jev's check, what it replaced or contradicts, its things and nearest facts |
| `GET /agents/{name}/detail` | The agent panel: now, last results, tools, level, spend |
| `GET /orders`, `GET /orders/{id}/path` | The chat with the Chief of Staff, and one order's tree with its cost |

**Agent states** come from the data: *stopped* (switched off, or its
department is), *paused* (the pause is on, or a run waits on budget or a
switch), *waiting* (an approval is pending), *working* (a run is running, or
will carry on by itself), else *idle*.

**"Today"** is the budget day (from midnight UTC, 8 pm New York), the day the
gateway enforces budgets on, so spend and budget always agree.

**The chat needs no new table:** it is the owner's orders to the Chief of
Staff, the `order_routed` events, the `route_order` questions and the
results. The events the design names map onto what is written today
(`tool_called`, `fact_write_decided`, `task_*`, `order_routed`,
`order_question`, `approval_*`, `run_paused`, `paused`, `killed`).

## Consequences

- No model is called to draw the map. Naming costs one cheap call per new
  neighbourhood, a fraction of a cent a day, inside the librarian's
  $0.25/day budget. Realtime is included in Supabase's free plan (200
  connections, 2 million messages a month); the owner is one viewer.
- The first map is drawn by `scripts.brain layout` (free) or the librarian's
  next run. `--reset` redraws it (every fact moves; the owner's call only).
- About 80% of events are model calls and judgments; the screen does not
  animate them. Spend reaches it through the pulse.
- The snapshot grows with the brain (about a megabyte at 10,000 facts). If
  that becomes slow, the facts list moves to its own paged endpoint.
- Held claims are not facts yet, so they have no place; the screen shows
  them on the rim as waiting.
