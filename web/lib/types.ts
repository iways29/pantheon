/**
 * What the API returns for the brain screen (api/app/screen.py, ADR 036).
 * Keep in step with the Python side; these are its shapes, not a design.
 */

export type RunState = 'running' | 'paused';

export interface Status {
  state: RunState;
  since: string | null;
  last_kill_at: string | null;
}

export interface NeedsYouCounts {
  questions: number;
  held_facts: number;
  approvals: number;
  changed_tools: number;
}

export interface Pulse {
  day_starts_at: string;
  spend_usd: number;
  budget_usd: number;
  facts_added: number;
  facts_rejected: number;
  tasks_done: number;
  tasks_failed: number;
  tasks_open: number;
  needs_you: NeedsYouCounts;
  needs_you_total: number;
}

export type AgentState = 'idle' | 'working' | 'waiting' | 'paused' | 'stopped';

export interface Agent {
  id: string;
  name: string;
  role: 'chief_of_staff' | 'head' | 'worker';
  runner: string;
  tier: string;
  level: string;
  enabled: boolean;
  department_id: string | null;
  department: string | null;
  state: AgentState;
  current_task: { id: string; title: string; status: string } | null;
  budget_usd: number | null;
  spent_today_usd: number;
  tools: string[];
}

export interface Department {
  id: string;
  name: string;
  enabled: boolean;
  head: string | null;
  budget_usd: number;
  spent_today_usd: number;
}

export type FactStatus = 'active' | 'disputed' | 'superseded';

export interface MapFact {
  id: string;
  claim: string;
  status: FactStatus;
  kind: string;
  public: boolean;
  at: string;
  x: number | null;
  y: number | null;
  n: string | null;
  agent: string | null;
}

export interface Neighbourhood {
  id: string;
  x: number;
  y: number;
  label: string;
  named_by: 'model' | 'entity' | 'claim';
  size: number;
}

export interface HeldClaim {
  approval_id: string;
  claim: string;
  recommendation: string | null;
  at: string;
  agent: string | null;
}

export interface Snapshot {
  status: Status;
  pulse: Pulse;
  departments: Department[];
  agents: Agent[];
  neighbourhoods: Neighbourhood[];
  facts: MapFact[];
  held: HeldClaim[];
}

/** A pending approval with its decision card (GET /approvals). */
export interface Approval {
  id: string;
  action_type: string;
  action_key: string | null;
  agent_id: string | null;
  agent: string | null;
  task_id: string | null;
  task_title: string | null;
  payload: Record<string, unknown>;
  recommendation: 'approve' | 'reject' | 'look_closer' | null;
  recommendation_probs: Record<string, number> | null;
  explanation: string | null;
  facts_checked: unknown[] | null;
  similar_decisions: unknown[] | null;
  conflicts: { claim?: string; decided_at?: string }[] | null;
  created_at: string;
}

export interface McpTool {
  name: string;
  server: string;
  description: string;
  enabled: boolean;
  risk_class: string;
  suggested_risk: string | null;
  approved_at: string | null;
  changed: boolean;
}

/** One row of `events`, as Realtime and replay deliver it. */
export interface PantheonEvent {
  id: string;
  type: string;
  created_at: string;
  agent_id: string | null;
  run_id: string | null;
  payload: Record<string, unknown>;
}

/** Someone the owner can talk with (GET /chat, ADR 037). */
export interface Talker {
  name: string;
  role: 'chief_of_staff' | 'head';
  enabled: boolean;
  department: string | null;
  last: { text: string; role: 'owner' | 'agent'; at: string } | null;
}

export interface OrderQuestion {
  approval_id: string;
  status: string;
  text: string | null;
  recommended: string | null;
  options: { department: string; probability: number }[];
  verdict: string | null;
  at: string;
  decided_at: string | null;
}

/** Work started from the chat, as it stands now. */
export interface OrderCard {
  id: string;
  title: string;
  text: string;
  status: string;
  result: string | null;
  error: string | null;
  at: string;
  finished_at: string | null;
  routed: { department: string | null; head: string | null; at: string }[];
  questions: OrderQuestion[];
  cost_usd: number;
}

export interface ChatMessage {
  id: string;
  role: 'owner' | 'agent';
  text: string;
  at: string;
  order: OrderCard | null;
}
