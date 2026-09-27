# Pantheon UI: design handoff for Claude Code

Read this file first. It explains what is in this folder, how to read the design files, and how to build the real app from them.

## What's here

```
pantheon-design/
  HANDOFF.md                  ← this file
  briefs/
    brain-brief.md            ← the owner's original brief for The Brain
    control-center-brief.md   ← the owner's original brief for the Control Center
  screens/                    ← one JPEG per artboard (quality 85) (the visual target)
  source/                     ← the design source, one .dc.html per artboard, plus canvas.json
```

The screenshots were rendered without the web fonts, so type shows in fallback fonts. The real faces are **Marcellus** (headings) and **Hanken Grotesk** (everything else), both from Google Fonts. Layout, colour, spacing and copy in the screenshots are accurate.

## How to read the source files

The `.dc.html` files are mockups in a design-canvas format. **Treat them as a precise spec, not as code to ship.** Rebuild them as real components in the chosen stack.

- `<helmet><style>` holds the CSS. The `.pan.dark` / `.pan.light` blocks are **the design tokens**. Copy them exactly.
- `{{name}}` is a value computed in the `renderVals()` method in the `<script>` at the bottom of each file. That script also holds all the sample data.
- `<sc-for list="{{items}}" as="x">` is a loop; `<sc-if value="{{cond}}">` is a conditional.
- `<dc-import name="Field" mode="busy">` renders another file (`Field.dc.html`) with props. Many artboards are thin wrappers that import a main file with a different state.
- Inline `style="…"` holds exact sizes and spacing. Copy the numbers, don't round them.

### Where things live

| Source file | What it is |
|---|---|
| `Main.dc.html` | The whole desktop Brain screen. Its `mode` prop gives every state: rest, busy, needs, fact, agent, approval, paused, killconfirm, killed. Also takes `theme`, `company`, `zoom`, `panX`, `panY`, `open`. |
| `Field.dc.html` | **The brain itself:** the lens (facts), the 24-hour dial, the agent ring, department hubs, event threads and beads, zoom levels, minimap, and pan/zoom handlers. The core of the product. |
| `Phone.dc.html` | Phone Brain. `view` = pulse, approval, chat, kill. |
| `Events.dc.html` | **Motion spec:** how each of the 15 event types looks and moves, plus its reduced-motion version. |
| `ScaleNotes.dc.html` | Rules for growth: zoom levels, rim markers, stable positions, department hubs. |
| `CcShell.dc.html` | Control Center shell (top bar, section nav, pulse strip) and **the full Control Center style sheet**. |
| `Cc*.dc.html` | One file per Control Center section; see the table below. |
| `CcPatterns.dc.html` | The shared patterns, each defined once. |
| `CcPhone.dc.html` | Phone Control Center. `view` = log, routines, depts, recipients, mcp. |

### Screen index (JPEG → what it shows)

**Brain**
- `Main` at rest, evening · `Busy` · `NeedsYou` (approval, question and changed tool all waiting) · `Light` (light mode)
- `Fact`, `Agent`, `Approval`: the three detail panels
- `Paused` · `KillConfirm` (typed confirmation) · `Killed`
- `Phone`, `PhoneApproval`, `PhoneChat`, `PhoneKill`
- `Events`: the motion spec
- `ScaleOverview` (53 agents, about 12,000 facts, zoomed out) · `ScalePanned` (rim markers, departments open) · `ScaleClaims` (deepest zoom) · `ScaleNotes`
- `Field`: the brain on its own

**Control Center**
- `CcDepts` · `CcDeptsPreview` (preview before apply) · `CcDeptsHistory` (history drawer)
- `CcAgents` · `CcPrompts` · `CcRoutines` · `CcKnowledge` · `CcModels`
- `CcTools` (built-in) · `CcToolsMcp` (changed MCP tool, old vs new) · `CcToolsAssign` (agent × tool grid)
- `CcAutonomy` · `CcJudge` · `CcRules` · `CcMail` · `CcLog`
- `CcPatterns` · `CcAgentsLight` · `CcShell` (first-run empty state)
- `CcPhone`, `CcPhoneRoutines`, `CcPhoneDepts`, `CcPhoneEmail`, `CcPhoneTools`

## Design rules that must survive the rebuild

1. **Only real events move things.** Every glow, thread and bead comes from one event in the live stream. No decorative loops. In the mockups the beads loop only so the motion is visible; in the product each runs once per event. After an event, a neighbourhood's glow fades over about 90 seconds.
2. **Status is never shown by colour alone.**

   | Shape | Meaning | Colour |
   |---|---|---|
   | Filled circle | active fact / working | gold |
   | Circle with a dashed ring | disputed | vermilion |
   | Hollow circle | superseded / idle | grey |
   | Diamond | waiting for the owner | ice blue |
   | Two bars | paused | grey |
   | Square | stopped | grey |

   Colour only confirms the shape: gold = knowledge, ice = needs you, vermilion = conflict. Over-budget bars are striped, not just red.
3. **Liquid Glass goes only on the controls layer:** nav, tabs, drawers, dialogs, sticky action bars and the brain's lens.
   - Lists, forms and tables stay on flat surfaces.
   - Never glass on glass: a nested glass element gets a flat fill.
   - Glass needs content behind it to bend.
   - In CSS, glass is a backdrop blur with saturation, a lit gradient edge (a mask trick) and a soft highlight.
   - Honour `prefers-reduced-transparency` (frost it nearly solid) and `prefers-contrast: more` (give it a solid edge).
4. **Reduced motion** must work everywhere. Each event type in `Events.dc.html` has a reduced-motion version.
5. **Voice:** few words, plain verbs, sentence case, and no Sanskrit anywhere in the interface. Keep the copy from the mockups unless there's a reason to change it.
6. **Control Center safety patterns:**
   - Everything is versioned.
   - Anything that creates or removes things shows a preview first.
   - New things are created switched off.
   - Every change goes to the log.
   - Locked rules look locked (hatched surface, lock icon, one-line reason).
   - Cost sits beside every setting that spends money.

## Engineering guidance

- **The brain must render with Canvas2D or WebGL** (PixiJS, regl or deck.gl), not DOM nodes. The mockup uses DOM elements, which is fine for 400 points and won't hold at 10,000+.
  - Draw only what's inside the lens.
  - Level of detail by zoom: below 0.7 draw neighbourhood clouds with counts; from 0.7 to 2.2 draw points; above 2.2 draw points plus claim labels for the nearest few.
  - Keep the lens chrome (dial, agent ring, glass rim) as SVG/DOM over the canvas.
- **Positions come from embeddings.** Project once (UMAP or similar) and store x,y per fact. New facts are placed beside their nearest neighbours; existing points never move. New topics grow at the edge of the space.
- **Live events** arrive over a websocket or server-sent events. The UI keeps a short event buffer; each event type maps to one animation (see `Events.dc.html`). Replay scrubs the same buffer by time.
- **Pause and Kill** are always visible. Kill pauses everything first, then asks for a typed "kill".

### Data shapes (from the brief; adjust to the backend)

```ts
type FactStatus = 'active' | 'disputed' | 'superseded' | 'held';
interface Fact { id: string; claim: string; source: string; sourcePage?: string; status: FactStatus;
  admittedAt: string; foundBy: string; x: number; y: number; neighbourhood: string;
  replaces?: string[]; contradicts?: string[]; jevCheck?: { verdict: string; note: string } }
type AgentState = 'idle' | 'working' | 'waiting' | 'paused' | 'stopped';
interface Agent { id: string; name: string; department: string; role: 'head' | 'worker' | 'chief_of_staff';
  state: AgentState; runner: 'deep' | 'pipeline' | 'router' | 'digest'; tier: 'cheap' | 'standard' | 'frontier';
  level: 'L0' | 'L1' | 'L2' | 'L3'; budgetDaily: number; spendToday: number; enabled: boolean }
interface Task { id: string; parentId?: string; orderId: string; agentId: string;
  status: 'started' | 'waiting' | 'done' | 'failed'; cost: number }
interface Approval { id: string; agentId: string; title: string; draft?: string;
  jev: { recommendation: 'approve' | 'reject' | 'look_closer'; reason: string };
  factsChecked: { factId: string; ok: boolean }[]; clashesWith?: { decisionId: string; text: string; date: string } }
type EventType = 'tool_used' | 'fact_admitted' | 'fact_rejected' | 'fact_held' | 'task_started' | 'task_waiting'
  | 'task_done' | 'task_failed' | 'order_routed' | 'question_for_owner' | 'approval_requested'
  | 'approval_decided' | 'run_paused' | 'paused' | 'killed';
interface PantheonEvent { id: string; type: EventType; at: string; agentId?: string; factId?: string; taskId?: string; orderId?: string }
interface ChangeLogEntry { id: string; at: string; by: 'owner' | string; screen: string; summary: string; before?: unknown; after?: unknown; undoable: boolean }
interface Version<T> { id: string; n: number; live: boolean; by: string; at: string; note?: string; body: T }
```

## Build order

1. Tokens (both themes), fonts, the glass surface, status marks, and the shared controls (toggle, tag, seg control, chip, button, input, locked rule, cost display).
2. App shell, the Brain / Control Center tabs, Pause and Kill (including the typed kill flow), and the pulse strip.
3. The brain on canvas/WebGL with mock data: lens, dial, agent ring, department hubs open and closed, pan and zoom, levels of detail, rim markers, minimap.
4. The event stream driving motion, then the fact, agent and approval panels and the chat panel with the order path.
5. Control Center shared patterns (versions and compare, preview before apply, history drawer, company log), then each section in the order of the brief.
6. Phone layouts for both tabs.
7. An accessibility pass (keyboard, focus rings, contrast, reduced motion, reduced transparency), and screenshot comparison against `screens/`.

## Placeholders to replace with real data

- Model names, per-token prices and the search provider appear as `[placeholder]` in `CcModels` and `CcTools`.
- Companies (Vireo, Ashline, Quillgate, Tern Labs, Ossify), dollar amounts, source domains and email addresses are illustrative sample data.

## Known gaps in the mockups

- `CcDepts`: the charter's action bar (Switch off, Apply, Publish v8) sits slightly below the visible area. In the real app the editor should scroll with a sticky action bar.
- The brain in the mockup is DOM-based and loops its beads for display. See the engineering guidance above.
