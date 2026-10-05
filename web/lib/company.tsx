'use client';

/**
 * The company as the screen knows it: loaded once, then kept current by the
 * live event stream (Supabase Realtime on `events`, ADR 036).
 *
 * Every change on screen comes from a real event: an event arrives, the parts
 * it can change are read again. Model calls and judgments are too many to
 * animate; the spend they add reaches the pulse on a slow timer instead.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import { api } from '@/lib/api';
import { previewSnapshot, type PreviewState } from '@/lib/fixtures';
import { createClient } from '@/lib/supabase/client';
import type {
  Agent,
  Approval,
  Department,
  McpTool,
  PantheonEvent,
  Pulse,
  Snapshot,
  Status,
} from '@/lib/types';

/** Events the screen does not react to one by one (as the API's QUIET). */
export const QUIET_EVENTS = new Set(['model_call', 'judgment_made', 'run_step', 'run_invoked']);
const STATUS_EVENTS = new Set(['paused', 'unpaused', 'killed']);
const PULSE_EVERY_MS = 60_000;
const SETTLE_MS = 1_200;

export type Live = 'connecting' | 'live' | 'offline';

interface CompanyState {
  snapshot: Snapshot | null;
  status: Status | null;
  pulse: Pulse | null;
  agents: Agent[];
  departments: Department[];
  approvals: Approval[];
  changedTools: McpTool[];
  live: Live;
  error: string | null;
}

interface Company extends CompanyState {
  /** Set on the local preview page only: sample data, no API, no live stream. */
  preview: PreviewState | null;
  /** Pause everything (resumable) or lift the pause. */
  setPause: (on: boolean) => Promise<void>;
  /** Cancel every unfinished task, run and held action for good. */
  kill: (note?: string) => Promise<void>;
  refresh: () => Promise<void>;
  /** Subscribe to each live event as it arrives (for motion). */
  onEvent: (listener: (event: PantheonEvent) => void) => () => void;
}

const CompanyContext = createContext<Company | null>(null);

export function useCompany(): Company {
  const company = useContext(CompanyContext);
  if (!company) throw new Error('useCompany must be used inside <CompanyProvider>');
  return company;
}

function initial(preview: PreviewState | null): CompanyState {
  const empty: CompanyState = {
    snapshot: null,
    status: null,
    pulse: null,
    agents: [],
    departments: [],
    approvals: [],
    changedTools: [],
    live: 'connecting',
    error: null,
  };
  if (!preview) return empty;
  const { snapshot, approvals, changedTools } = previewSnapshot(preview);
  return {
    ...empty,
    snapshot,
    status: snapshot.status,
    pulse: snapshot.pulse,
    agents: snapshot.agents,
    departments: snapshot.departments,
    approvals,
    changedTools,
    live: 'live',
  };
}

export function CompanyProvider({
  children,
  preview = null,
}: {
  children: ReactNode;
  preview?: PreviewState | null;
}) {
  const [state, setState] = useState<CompanyState>(() => initial(preview));
  const listeners = useRef(new Set<(event: PantheonEvent) => void>());
  const settle = useRef<ReturnType<typeof setTimeout> | null>(null);

  // A refresh that brings back what is already shown changes nothing, so the
  // screen is not drawn again: most refreshes after an event are like that.
  const patch = useCallback((next: Partial<CompanyState>) => {
    setState((prev) => {
      const changed = (Object.keys(next) as (keyof CompanyState)[]).filter(
        (key) => JSON.stringify(prev[key]) !== JSON.stringify(next[key]),
      );
      if (!changed.length) return prev;
      return { ...prev, ...Object.fromEntries(changed.map((key) => [key, next[key]])) };
    });
  }, []);

  const loadAll = useCallback(async () => {
    try {
      const [snapshot, approvals, tools] = await Promise.all([
        api.get<Snapshot>('screen/snapshot'),
        api.get<Approval[]>('approvals'),
        api.get<McpTool[]>('mcp/tools').catch(() => [] as McpTool[]),
      ]);
      patch({
        snapshot,
        status: snapshot.status,
        pulse: snapshot.pulse,
        agents: snapshot.agents,
        departments: snapshot.departments,
        approvals,
        changedTools: tools.filter((t) => t.changed),
        error: null,
      });
    } catch (error) {
      patch({ error: error instanceof Error ? error.message : String(error) });
    }
  }, [patch]);

  /** What an event can change, read again: never the whole map of facts. */
  const loadMoving = useCallback(async () => {
    try {
      const [status, pulse, people, approvals] = await Promise.all([
        api.get<Status>('screen/status'),
        api.get<Pulse>('screen/pulse'),
        api.get<{ agents: Agent[]; departments: Department[] }>('screen/agents'),
        api.get<Approval[]>('approvals'),
      ]);
      patch({ status, pulse, agents: people.agents, departments: people.departments, approvals });
    } catch (error) {
      patch({ error: error instanceof Error ? error.message : String(error) });
    }
  }, [patch]);

  const settleSoon = useCallback(
    (now = false) => {
      if (settle.current) clearTimeout(settle.current);
      settle.current = setTimeout(() => void loadMoving(), now ? 0 : SETTLE_MS);
    },
    [loadMoving],
  );

  useEffect(() => {
    if (!preview) void loadAll();
  }, [loadAll, preview]);

  // The live stream. RLS applies per subscriber: only this org's events.
  useEffect(() => {
    if (preview) return;
    const supabase = createClient();
    const channel = supabase
      .channel('pantheon-events')
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'events' },
        (message) => {
          const event = message.new as PantheonEvent;
          if (QUIET_EVENTS.has(event.type)) return;
          // Events go to the listeners (motion, the chat, the dial), not into
          // the shared state: that would draw the whole screen again for each.
          for (const listener of listeners.current) listener(event);
          settleSoon(STATUS_EVENTS.has(event.type));
        },
      )
      .subscribe((status) => {
        if (status === 'SUBSCRIBED') patch({ live: 'live' });
        else if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT' || status === 'CLOSED')
          patch({ live: 'offline' });
      });
    return () => {
      void supabase.removeChannel(channel);
    };
  }, [patch, settleSoon, preview]);

  // The preview has no live stream: a sample one, a read or a write every few
  // seconds naming real sample facts, so the brain's threads can be checked.
  const sample = useRef(state);
  sample.current = state;
  useEffect(() => {
    if (!preview || preview === 'killed') return;
    let n = 0;
    const timer = setInterval(() => {
      const { snapshot } = sample.current;
      const facts = snapshot?.facts ?? [];
      const agents = snapshot?.agents ?? [];
      if (!facts.length || !agents.length) return;
      n += 1;
      const agent = agents[n % agents.length]!;
      const pick = (k: number) => facts[(n * 7 + k * 13) % facts.length]!.id;
      const write = n % 3 === 0;
      const event: PantheonEvent = {
        id: `sample-${n}`,
        type: write ? 'fact_write_decided' : 'tool_called',
        created_at: new Date().toISOString(),
        agent_id: agent.id,
        run_id: null,
        payload: write
          ? { outcome: 'duplicate', fact_id: pick(0) }
          : { tool: 'brain_search', fact_ids: [pick(0), pick(1)] },
      };
      for (const listener of listeners.current) listener(event);
    }, 3500);
    return () => clearInterval(timer);
  }, [preview]);

  // Spend moves with model calls, which are not animated: a slow refresh.
  useEffect(() => {
    if (preview) return;
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') void loadMoving();
    }, PULSE_EVERY_MS);
    return () => clearInterval(timer);
  }, [loadMoving, preview]);

  const setPause = useCallback(
    async (on: boolean) => {
      if (preview) {
        const now = new Date().toISOString();
        setState((prev) => ({ ...prev, status: { state: on ? 'paused' : 'running', since: now, last_kill_at: prev.status?.last_kill_at ?? null } }));
        return;
      }
      await api.post('pause', { on });
      await loadMoving();
    },
    [loadMoving, preview],
  );

  const kill = useCallback(
    async (note?: string) => {
      if (preview) {
        const now = new Date().toISOString();
        setState((prev) => ({ ...prev, status: { state: 'paused', since: prev.status?.since ?? now, last_kill_at: now } }));
        return;
      }
      await api.post('kill', { confirm: 'KILL', note: note ?? null });
      await loadMoving();
    },
    [loadMoving, preview],
  );

  const onEvent = useCallback((listener: (event: PantheonEvent) => void) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const value = useMemo<Company>(
    () => ({ ...state, preview, setPause, kill, refresh: loadAll, onEvent }),
    [state, preview, setPause, kill, loadAll, onEvent],
  );

  return <CompanyContext.Provider value={value}>{children}</CompanyContext.Provider>;
}

/** Stopped for good: the last kill came after the pause was last switched. */
export function isKilled(status: Status | null): boolean {
  if (!status || status.state !== 'paused' || !status.last_kill_at) return false;
  return !status.since || status.last_kill_at >= status.since;
}
