/**
 * Sample data for the local preview (app/preview): the design's company at a
 * glance, so the screens can be compared with docs/design/pantheon-design
 * without signing in or touching the live API. Never used in production.
 * Companies and amounts are made up, as in the mockups.
 */

import type { Agent, Approval, ChatMessage, McpTool, OrderCard, PantheonEvent, Snapshot, Talker } from '@/lib/types';

export type PreviewState = 'rest' | 'busy' | 'needs' | 'paused' | 'killed';

/** A small deterministic random, so the map is the same on every load. */
function random(seed: number): () => number {
  let s = seed;
  return () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
}

const HOODS: [string, number, number][] = [
  ['Commitments', 0.12, -0.62],
  ['Your decisions', -0.42, -0.5],
  ['Sources', 0.02, -0.28],
  ['Brand voice', -0.62, -0.02],
  ['Security market', 0.36, -0.18],
  ['Funding rounds', 0.44, 0.14],
  ['Audience', -0.5, 0.28],
  ['Pricing', -0.06, 0.24],
  ['Competitors', 0.3, 0.46],
];

const DAY = '2026-09-27';
const at = (hm: string) => new Date(`${DAY}T${hm}:00-04:00`).toISOString();

function agent(
  name: string,
  department: string,
  role: Agent['role'],
  state: Agent['state'],
  spent: number,
): Agent {
  return {
    id: `a-${name}`,
    name,
    role,
    runner: role === 'chief_of_staff' ? 'router' : 'deep',
    tier: 'cheap',
    level: 'L2',
    enabled: state !== 'stopped',
    department_id: `d-${department}`,
    department,
    state,
    current_task: state === 'working' ? { id: 't1', title: 'Funding news', status: 'running' } : null,
    budget_usd: null,
    spent_today_usd: spent,
    tools: ['web_search', 'read_page', 'brain_search'],
  };
}

export function previewSnapshot(state: PreviewState): {
  snapshot: Snapshot;
  approvals: Approval[];
  changedTools: McpTool[];
} {
  const rnd = random(7);
  const facts: Snapshot['facts'] = [];
  const neighbourhoods: Snapshot['neighbourhoods'] = HOODS.map(([label, x, y], i) => ({
    id: `n${i}`,
    x,
    y,
    z: Math.sin(i * 2.1) * 0.45,
    label,
    named_by: 'model' as const,
    size: 0,
  }));
  for (let i = 0; i < 420; i++) {
    const hood = neighbourhoods[i % neighbourhoods.length]!;
    const angle = rnd() * Math.PI * 2;
    const r = Math.sqrt(rnd()) * 0.2;
    const depth = (rnd() - 0.5) * 0.3;
    const roll = rnd();
    hood.size += 1;
    facts.push({
      id: `f${i}`,
      claim: `A fact about ${hood.label.toLowerCase()}, number ${i + 1}.`,
      status: roll < 0.03 ? 'disputed' : roll < 0.2 ? 'superseded' : 'active',
      kind: 'fact',
      public: false,
      at: at('09:00'),
      x: hood.x + Math.cos(angle) * r,
      y: hood.y + Math.sin(angle) * r,
      z: hood.z + depth,
      n: hood.id,
      agent: null,
    });
  }

  const busy = state === 'busy' || state === 'needs';
  const halted = state === 'paused' || state === 'killed';
  const work = (s: Agent['state']): Agent['state'] =>
    state === 'killed' ? 'stopped' : state === 'paused' ? 'paused' : busy ? s : 'idle';
  const agents = [
    agent('chief-of-staff', 'executive', 'chief_of_staff', state === 'needs' ? 'waiting' : work('working'), 0.07),
    agent('briefing-writer', 'executive', 'worker', 'idle', 0.71),
    agent('ledger-keeper', 'executive', 'worker', 'idle', 0.18),
    agent('research-head', 'research', 'head', work('working'), 0.07),
    agent('market-scanner', 'research', 'worker', work('working'), 0.98),
    agent('source-reader', 'research', 'worker', work('working'), 0.75),
    agent('analyst', 'research', 'worker', 'idle', 0),
    agent('archivist', 'research', 'worker', 'idle', 0),
    agent('marketing-head', 'marketing', 'head', state === 'needs' ? 'waiting' : 'idle', 0.3),
    agent('copywriter', 'marketing', 'worker', 'idle', 0),
    agent('social-planner', 'marketing', 'worker', 'idle', 0),
    agent('jev', 'executive', 'worker', 'idle', 0.02),
  ];
  const departments = ['executive', 'research', 'marketing'].map((name) => ({
    id: `d-${name}`,
    name,
    enabled: true,
    head: agents.find((a) => a.department === name && a.role !== 'worker')?.name ?? null,
    budget_usd: 5,
    spent_today_usd: agents.filter((a) => a.department === name).reduce((s, a) => s + a.spent_today_usd, 0),
  }));

  const needs = state === 'needs' || halted;
  const approvals: Approval[] = needs
    ? [
        {
          id: 'ap1',
          action_type: 'publish_post',
          action_key: null,
          agent_id: 'a-marketing-head',
          agent: 'marketing-head',
          task_id: null,
          task_title: null,
          payload: { title: 'Publish the Ashvas beta post on LinkedIn' },
          recommendation: 'look_closer',
          recommendation_probs: null,
          explanation: 'It announces a public launch, which you ruled out.',
          facts_checked: [],
          similar_decisions: [],
          conflicts: [{ claim: 'No public launch claims yet', decided_at: '2026-09-12T15:00:00Z' }],
          created_at: at('14:29'),
        },
        {
          id: 'ap2',
          action_type: 'route_order',
          action_key: 'route_order',
          agent_id: 'a-chief-of-staff',
          agent: 'chief-of-staff',
          task_id: 't0',
          task_title: 'Find three competitors',
          payload: { order: 'Find three competitors to Ashvas that raised money this year.' },
          recommendation: 'look_closer',
          recommendation_probs: null,
          explanation: 'Does “this year” mean 2026 so far, or the last 12 months?',
          facts_checked: null,
          similar_decisions: null,
          conflicts: null,
          created_at: at('14:31'),
        },
      ]
    : [];
  const changedTools: McpTool[] = needs
    ? [
        {
          name: 'web_search',
          server: 'search',
          description: 'Search the web',
          enabled: false,
          risk_class: 'R2',
          suggested_risk: 'R3',
          approved_at: at('09:00'),
          changed: true,
        },
      ]
    : [];

  const pulses = {
    rest: [6.4, 52, 9, 21, 0],
    busy: [3.46, 31, 5, 11, 5],
    needs: [4.12, 38, 6, 14, 5],
    paused: [4.12, 38, 6, 14, 5],
    killed: [4.14, 38, 6, 14, 0],
  }[state];
  const snapshot: Snapshot = {
    status: {
      state: halted ? 'paused' : 'running',
      since: halted ? at('14:36') : null,
      last_kill_at: state === 'killed' ? at('14:38') : null,
    },
    pulse: {
      day_starts_at: at('20:00'),
      spend_usd: pulses[0]!,
      budget_usd: 15,
      facts_added: pulses[1]!,
      facts_rejected: pulses[2]!,
      tasks_done: pulses[3]!,
      tasks_failed: 0,
      tasks_open: pulses[4]!,
      needs_you: {
        questions: needs ? 1 : 0,
        held_facts: 0,
        approvals: needs ? 1 : 0,
        changed_tools: needs ? 1 : 0,
      },
      needs_you_total: needs ? 3 : 0,
    },
    departments,
    agents,
    neighbourhoods,
    facts,
    held: needs
      ? [{ approval_id: 'h1', claim: 'Vireo raised a seed round', recommendation: 'look_closer', at: at('13:10'), agent: null }]
      : [],
  };
  return { snapshot, approvals, changedTools };
}

export function previewTalkers(): Talker[] {
  return [
    { name: 'chief-of-staff', role: 'chief_of_staff', enabled: true, department: 'executive', last: null },
    { name: 'research-head', role: 'head', enabled: true, department: 'research', last: null },
    { name: 'marketing-head', role: 'head', enabled: true, department: 'marketing', last: null },
  ];
}

export function previewThread(who: string, state: PreviewState): ChatMessage[] {
  if (who !== 'chief-of-staff') return [];
  const order: OrderCard = {
    id: 'o2',
    title: 'Find three competitors to Ashvas',
    text: 'Find three competitors to Ashvas that raised money this year.',
    status: state === 'rest' ? 'done' : state === 'killed' ? 'cancelled' : 'blocked',
    result:
      state === 'rest'
        ? 'Vireo, Ashline and Tern Labs each raised this year; the brief has the rounds.\n\n' +
          '- **Vireo:** a $6M seed in March. https://techcrunch.com/2026/03/12/vireo-raises-seed-to-build-ai-claims-triage-for-insurers-and-brokers/\n' +
          '- **Ashline:** a $4M pre-seed in June [Unverified: the month; one source says May], led by a former Ashvas engineer.\n' +
          'Team notes: web-researcher added 6 facts, none rejected or held; fact-curator found nothing stale. No sub-task failed.'
        : null,
    error: null,
    at: at('14:02'),
    finished_at: state === 'rest' ? at('18:40') : null,
    routed: [{ department: 'research', head: 'research-head', at: at('14:03') }],
    questions:
      state === 'needs' || state === 'paused'
        ? [
            {
              approval_id: 'ap2',
              status: 'pending',
              text: 'Does “this year” mean 2026 so far, or the last 12 months?',
              recommended: 'research',
              options: [
                { department: 'research', probability: 0.62 },
                { department: 'marketing', probability: 0.21 },
              ],
              verdict: null,
              at: at('14:31'),
              decided_at: null,
            },
          ]
        : [],
    cost_usd: 1.84,
  };
  return [
    { id: 'm1', role: 'owner', text: 'Morning. Anything I should know?', at: at('09:02'), order: null },
    {
      id: 'm2',
      role: 'agent',
      text: 'Morning, boss. Quiet night: the brief went out at 07:15 and nothing waits for you.',
      at: at('09:02'),
      order: null,
    },
    {
      id: 'm3',
      role: 'owner',
      text: 'Find three competitors to Ashvas that raised money this year.',
      at: at('14:02'),
      order,
    },
    { id: 'm4', role: 'agent', text: 'On it: I have handed it to Research.', at: at('14:02'), order },
    ...(state === 'rest' ? songOrder() : []),
  ];
}

/** An order that made a piece: the chat shows it in full (2026-09-27). */
function songOrder(): ChatMessage[] {
  const order: OrderCard = {
    id: 'o3',
    title: 'Write company song',
    text: 'understand the company knowledge and build me a song',
    status: 'done',
    result:
      'content-lead: I picked “The Charioteer at the Edge of the Field” from three ideas and the writer drafted it. ' +
      'The claim check stopped two lines as not in the public facts, so it is not ready to publish; ' +
      'the song itself is above. Nothing has been published.',
    error: null,
    at: at('21:13'),
    finished_at: at('21:23'),
    routed: [{ department: 'marketing', head: 'marketing-head', at: at('21:13') }],
    questions: [],
    pieces: [
      {
        id: 'p1',
        title: 'The Charioteer at the Edge of the Field',
        status: 'blocked',
        channel: 'other',
        format: 'company_song',
        body:
          '**Verse 1**\nAt the edge of the field,\nBefore the company, before the deck,\nOne choice carries weight.\n' +
          'The founder holds the bow.\n\n**Chorus**\nYou choose the road.\nYou remain in the lead.\n' +
          'We bring architecture, people and hours,\nIntroductions and launch help for the need.\n\n' +
          '**Verse 2**\nAn enterprise buyer joins the room.\nAn operator tests the plan.\nThe founder leads. The studio serves.',
        stopped: [
          { sentence: 'You remain in the lead.', reason: "Not in the brain's public facts" },
          { sentence: 'An operator tests the plan.', reason: "Not in the brain's public facts" },
        ],
      },
    ],
    cost_usd: 0.05,
  };
  return [
    { id: 'm5', role: 'owner', text: order.text, at: at('21:13'), order },
    { id: 'm6', role: 'agent', text: 'On it: Write company song. I will report back here.', at: at('21:13'), order },
  ];
}

/**
 * A sample day for the preview's Replay: reads and writes naming real sample
 * facts, spread from 06:30 to now, as the mornings run.
 */
export function previewDay(
  facts: { id: string }[],
  agents: { id: string }[],
  dayStart: number,
): PantheonEvent[] {
  if (!facts.length || !agents.length) return [];
  const now = Date.now() - 60 * 1000;
  // From 06:30, or across the whole day so far when it is still early.
  const from = Math.min(dayStart + 6.5 * 3600 * 1000, dayStart + (now - dayStart) * 0.1);
  const to = Math.max(from + 60 * 1000, now);
  const n = 90;
  return Array.from({ length: n }, (_, i) => {
    const write = i % 4 === 0;
    const agent = agents[(i * 5) % agents.length]!;
    const pick = (k: number) => facts[(i * 11 + k * 17) % facts.length]!.id;
    return {
      id: `day-${i}`,
      type: write ? 'fact_write_decided' : 'tool_called',
      created_at: new Date(from + ((to - from) * i) / n).toISOString(),
      agent_id: agent.id,
      run_id: null,
      payload: write ? { outcome: 'duplicate', fact_id: pick(0) } : { tool: 'brain_search', fact_ids: [pick(0), pick(1)] },
    };
  });
}

/** An order's path for the preview's "Follow this order": the Chief of Staff
 * to a head to two workers, and a few sample facts they touched. */
export function previewPath(
  id: string,
  facts: { id: string }[],
  agents: { id: string; role: string; department_id: string | null }[],
) {
  const chief = agents.find((a) => a.role === 'chief_of_staff') ?? agents[0];
  const head = agents.find((a) => a.role === 'head') ?? agents[1];
  const workers = agents.filter((a) => a.role === 'worker' && a.department_id === head?.department_id).slice(0, 2);
  const steps = [
    { id: `${id}:0`, parent_id: null, agent_id: chief?.id ?? null, status: 'blocked' },
    { id: `${id}:1`, parent_id: `${id}:0`, agent_id: head?.id ?? null, status: 'blocked' },
    ...workers.map((w, i) => ({ id: `${id}:w${i}`, parent_id: `${id}:1`, agent_id: w.id, status: 'running' })),
  ];
  const doers = workers.length ? workers : [head];
  const picked = facts.slice(40, 49);
  return {
    id,
    steps,
    facts: picked.map((f, i) => ({
      fact_id: f.id,
      way: (i % 3 === 0 ? 'in' : 'out') as 'in' | 'out',
      agent_id: doers[i % doers.length]?.id ?? null,
    })),
  };
}
