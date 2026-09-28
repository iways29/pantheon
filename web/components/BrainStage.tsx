'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { Dial, type Thread } from '@/components/brain/Dial';
import { type BrainFilter, NO_FILTER, windowStart } from '@/components/brain/Filters';
import { Minimap } from '@/components/brain/Minimap';
import { ReplayBar, useReplay } from '@/components/brain/Replay';
import { Globe, type GlobeHandle, type View } from '@/components/brain/Globe';
import { AgentPanel, FactPanel } from '@/components/brain/Panels';
import { Icon } from '@/components/Icon';
import { FACT_MARK, Mark } from '@/components/Mark';
import { api } from '@/lib/api';
import { isKilled, useCompany } from '@/lib/company';
import { previewDay } from '@/lib/fixtures';
import { count, time } from '@/lib/format';
import type { MapFact, PantheonEvent } from '@/lib/types';

/** How long a thread between the brain and an agent stays drawn. */
const THREAD_MS = 2600;
/** Tools that read the brain: a thread runs out to the agent. */
const READS = new Set(['brain_search', 'brain_recall', 'brain_read']);
/** Threads drawn for one read: enough to show where, not a hairball. */
const THREADS_PER_EVENT = 3;

function ids(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

/** Midnight in New York, as an instant: where the dial's day starts. */
function startOfDay(): string {
  const now = new Date();
  const ny = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const offset = now.getTime() - ny.getTime();
  ny.setHours(0, 0, 0, 0);
  return new Date(ny.getTime() + offset).toISOString();
}

interface FactPlace {
  id: string;
  claim: string;
  status: MapFact['status'];
  kind: string;
  public: boolean;
  at: string;
  place: { x: number; y: number; z: number; neighbourhood_id: string | null } | null;
}

/**
 * The brain's stage: the globe, the dial and agent ring around it, the
 * panels for a fact or an agent, and the paused and stopped veils.
 */
export function BrainStage({ filter = NO_FILTER }: { filter?: BrainFilter }) {
  const { snapshot, agents, departments, status, pulse, setPause, onEvent, preview } = useCompany();
  const globe = useRef<GlobeHandle>(null);
  const stage = useRef<HTMLElement>(null);
  const [box, setBox] = useState({ width: 0, height: 0 });
  const [radius, setRadius] = useState(0);
  const [fact, setFact] = useState<string | null>(null);
  const [agent, setAgent] = useState<string | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [arrived, setArrived] = useState<MapFact[]>([]);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<View>({ azimuth: 0, zoom: 1 });
  const lastView = useRef(0);
  const [opened, setOpened] = useState<Set<string>>(new Set());
  const [key, setKey] = useState(false);

  const facts = useMemo(() => {
    const known = new Set((snapshot?.facts ?? []).map((f) => f.id));
    return [...(snapshot?.facts ?? []), ...arrived.filter((f) => !known.has(f.id))];
  }, [snapshot, arrived]);
  const held = snapshot?.held ?? [];
  const hoods = snapshot?.neighbourhoods ?? [];
  const killed = isKilled(status);
  const paused = status?.state === 'paused' && !killed;

  useEffect(() => {
    const el = stage.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setBox({ width: el.clientWidth, height: el.clientHeight }));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Today's work so far: the dial's ticks, and what Replay plays again.
  const dayStart = useMemo(() => Date.parse(startOfDay()), []);
  const [day, setDay] = useState<PantheonEvent[]>([]);
  useEffect(() => {
    if (preview) setDay(previewDay(snapshot?.facts ?? [], snapshot?.agents ?? [], dayStart));
  }, [preview, dayStart, snapshot?.facts, snapshot?.agents]);
  useEffect(() => {
    if (preview) return;
    api
      .get<{ id: string; type: string; at: string; agent_id: string | null; run_id: string | null; payload: Record<string, unknown> }[]>(
        `screen/events?since=${encodeURIComponent(new Date(dayStart).toISOString())}&limit=5000`,
      )
      .then((list) => setDay(list.map(({ at, ...e }) => ({ ...e, created_at: at }))))
      .catch(() => setDay([]));
  }, [preview, dayStart]);

  // One thread per fact the event names (a few at most), else one to the glass.
  const thread = useCallback((agentId: string | null, way: Thread['way'], factIds: string[] = []) => {
    if (!agentId) return;
    const stamp = performance.now();
    const made: Thread[] = (factIds.length ? factIds.slice(0, THREADS_PER_EVENT) : [undefined]).map(
      (factId, i) => ({ key: `${agentId}:${stamp}:${i}`, agentId, way, factId }),
    );
    const keys = new Set(made.map((t) => t.key));
    setThreads((list) => [...list.filter((t) => t.agentId !== agentId), ...made]);
    setTimeout(() => setThreads((list) => list.filter((t) => !keys.has(t.key))), THREAD_MS);
    // What an event touches glows, then fades (Events.dc.html).
    for (const id of factIds.slice(0, THREADS_PER_EVENT)) globe.current?.flash(id);
  }, []);

  // Every movement comes from one real event: live, or replayed.
  const show = useCallback(
    (event: PantheonEvent, replayed: boolean) => {
      const p = event.payload ?? {};
      if (event.type === 'tool_called' && READS.has(String(p.tool))) thread(event.agent_id, 'out', ids(p.fact_ids));
      // A chat reply reads the facts nearest the owner's message first.
      if (event.type === 'chat_recalled') thread(event.agent_id, 'out', ids(p.fact_ids));
      if (event.type === 'fact_write_decided') {
        const id = typeof p.fact_id === 'string' ? p.fact_id : null;
        thread(event.agent_id, 'in', id && p.outcome !== 'rejected' ? [id] : []);
        // A replayed fact is already drawn; the preview has no API.
        if (id && p.outcome !== 'rejected' && !preview && !replayed) {
          api
            .get<FactPlace>(`facts/${id}`)
            .then((f) => {
              if (!f.place) return;
              setArrived((list) => [
                ...list,
                {
                  id: f.id,
                  claim: f.claim.slice(0, 160),
                  status: f.status,
                  kind: f.kind,
                  public: f.public,
                  at: f.at,
                  x: f.place!.x,
                  y: f.place!.y,
                  z: f.place!.z,
                  n: f.place!.neighbourhood_id,
                  agent: event.agent_id,
                },
              ]);
              setTimeout(() => globe.current?.flash(f.id), 50);
            })
            .catch(() => undefined);
        }
      }
    },
    [thread, preview],
  );

  const replay = useReplay(day, dayStart, (event) => show(event, true));
  const replaying = replay.at !== null;
  const replayingRef = useRef(false);
  replayingRef.current = replaying;

  useEffect(
    () =>
      onEvent((event) => {
        setDay((list) => [...list, event]);
        // While replaying, the live stream is kept but not drawn over the past.
        if (!replayingRef.current) show(event, false);
      }),
    [onEvent, show],
  );

  // "Replay the day" in the pulse strip.
  const startReplay = replay.start;
  useEffect(() => {
    const onReplay = () => startReplay();
    window.addEventListener('pantheon:replay', onReplay);
    return () => window.removeEventListener('pantheon:replay', onReplay);
  }, [startReplay]);

  const moments = useMemo(
    () => day.filter((e) => !replaying || Date.parse(e.created_at) <= replay.at!).map((e) => e.created_at),
    [day, replaying, replay.at],
  );

  const byStatus = (s: MapFact['status']) => facts.filter((f) => f.status === s).length;

  // Open on the ring: what the owner opened, and the department filtered to.
  // Nothing opens or closes by itself.
  // The Executive Office starts open; after that the owner opens and closes.
  const started = useRef(false);
  useEffect(() => {
    const executive = departments.find((d) => d.name === 'executive');
    if (executive && !started.current) {
      started.current = true;
      setOpened((prev) => new Set(prev).add(executive.id));
    }
  }, [departments]);
  const open = useMemo(() => {
    const set = new Set(opened);
    if (filter.department) set.add(filter.department);
    return set;
  }, [opened, filter.department]);

  function toggle(id: string) {
    setOpened((prev) => {
      const next = new Set(prev);
      if (open.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // What a filter keeps bright; the rest of the brain is dimmed, not hidden.
  // Replaying, a fact not yet made at the replayed minute is dimmed.
  const replayMinute = replaying ? Math.floor(replay.at! / 60000) : null;
  const bright = useMemo(() => {
    if (filter.needsOnly) return new Set<string>();
    const since = windowStart(filter.window, pulse?.day_starts_at);
    const until = replayMinute === null ? null : (replayMinute + 1) * 60000;
    if (!filter.department && since === null && until === null) return null;
    const inDepartment = new Set(
      agents.filter((a) => a.department_id === filter.department).map((a) => a.id),
    );
    return new Set(
      facts
        .filter((f) => !filter.department || (f.agent !== null && inDepartment.has(f.agent)))
        .filter((f) => since === null || Date.parse(f.at) >= since)
        .filter((f) => until === null || Date.parse(f.at) < until)
        .map((f) => f.id),
    );
  }, [filter, facts, agents, pulse?.day_starts_at, replayMinute]);

  const onView = useCallback((next: View) => {
    const now = performance.now();
    if (now - lastView.current < 120) return;
    lastView.current = now;
    setView(next);
  }, []);

  async function resume() {
    setBusy(true);
    try {
      await setPause(false);
    } finally {
      setBusy(false);
    }
  }

  function talk(name: string) {
    window.dispatchEvent(new CustomEvent('pantheon:talk', { detail: name }));
    setAgent(null);
  }

  return (
    <main className="stage" aria-label="The brain" ref={stage}>
      <Globe
        ref={globe}
        facts={facts}
        held={held}
        neighbourhoods={hoods}
        selected={fact}
        onSelect={(id) => {
          setFact(id);
          if (id) setAgent(null);
        }}
        onRadius={setRadius}
        bright={bright}
        onView={onView}
      />
      <Dial
        width={box.width}
        height={box.height}
        radius={radius}
        agents={agents}
        departments={departments}
        open={open}
        onToggle={toggle}
        moments={moments}
        threads={threads}
        selected={agent}
        locate={(id) => globe.current?.locate(id) ?? null}
        handAt={replay.at}
        dayStart={dayStart}
        onScrub={(at) => (replaying ? replay.seek(at) : replay.start(at))}
        onSelect={(name) => {
          setAgent(name);
          setFact(null);
        }}
      />

      <div className="stage-title">
        <h1 className="disp" style={{ fontSize: 24 }}>
          The brain
        </h1>
        <span className="faint num" style={{ fontSize: 13 }}>
          {snapshot ? `${count(facts.length, 'fact')} in ${count(hoods.length, 'neighbourhood')}` : ' '}
        </span>
        <span className="faint num" style={{ fontSize: 13 }}>
          {snapshot ? `${count(agents.length, 'agent')} in ${count(departments.length, 'department')}` : ' '}
        </span>
      </div>

      <div className="glass zoom" role="group" aria-label="Zoom">
        <button className="btn ibtn sm" aria-label="Zoom out" onClick={() => globe.current?.zoom(1.25)}>
          −
        </button>
        <span className="faint" style={{ fontSize: 13 }}>
          Drag to turn
        </span>
        <button className="btn ibtn sm" aria-label="Zoom in" onClick={() => globe.current?.zoom(0.8)}>
          +
        </button>
        <button className="btn sm" onClick={() => globe.current?.fitAll()}>
          Fit all
        </button>
      </div>

      <Minimap facts={facts} azimuth={view.azimuth} zoom={view.zoom} onFit={() => globe.current?.fitAll()} />

      <div className="key">
        <button className="btn sm glass" aria-expanded={key} onClick={() => setKey((v) => !v)}>
          <span className="gl f-act" aria-hidden="true" />
          Key
          <Icon name="chevron" size={14} style={{ transform: key ? 'rotate(180deg)' : undefined }} />
        </button>
        {key ? (
          <div className="glass key-body">
      <div className="legend-grid" style={{ gridTemplateColumns: '12px auto auto' }}>
        <Mark kind={FACT_MARK.active} />
        <span>Active</span>
        <span className="faint num" style={{ textAlign: 'right' }}>{byStatus('active')}</span>
        <Mark kind={FACT_MARK.disputed} />
        <span>Disputed</span>
        <span className="faint num" style={{ textAlign: 'right' }}>{byStatus('disputed')}</span>
        <Mark kind={FACT_MARK.superseded} />
        <span>Superseded</span>
        <span className="faint num" style={{ textAlign: 'right' }}>{byStatus('superseded')}</span>
        <Mark kind={FACT_MARK.held} />
        <span>Held for you</span>
        <span className="faint num" style={{ textAlign: 'right' }}>{held.length}</span>
      </div>

      <div className="legend-grid" style={{ gridTemplateColumns: '12px auto' }}>
        <Mark kind="idle" />
        <span>Idle</span>
        <Mark kind="work" />
        <span>Working</span>
        <Mark kind="wait" />
        <span>Waiting for you</span>
        <Mark kind="pause" />
        <span>Paused</span>
        <Mark kind="stop" />
        <span>Stopped</span>
        <span className="hubmini" aria-hidden="true" />
        <span>Closed department</span>
      </div>

          </div>
        ) : null}
      </div>

      <ReplayBar replay={replay} count={day.length} />

      {fact ? <FactPanel id={fact} onClose={() => setFact(null)} onSelect={setFact} /> : null}
      {agent ? <AgentPanel name={agent} onClose={() => setAgent(null)} onTalk={talk} /> : null}

      {paused ? (
        <div className="veil">
          <div className="glass veil-card">
            <Mark kind="pause" style={{ width: 18, height: 22 }} />
            <h2 className="disp" style={{ fontSize: 26 }}>
              Everything is paused
            </h2>
            <p className="muted" style={{ fontSize: 14, lineHeight: 1.5 }}>
              {pulse?.tasks_open
                ? `${count(pulse.tasks_open, 'task')} ${pulse.tasks_open === 1 ? 'is' : 'are'} holding mid-step. `
                : ''}
              Nothing is lost, and nothing new starts until you resume.
            </p>
            <button className="btn pri" onClick={() => void resume()} disabled={busy} style={{ marginTop: 6 }}>
              <Icon name="play" />
              Resume all work
            </button>
          </div>
        </div>
      ) : null}

      {killed ? (
        <div className="veil">
          <div className="glass veil-card">
            <Mark kind="stop" style={{ width: 18, height: 18 }} />
            <h2 className="disp" style={{ fontSize: 26 }}>
              All work is stopped
            </h2>
            <p className="muted" style={{ fontSize: 14, lineHeight: 1.5 }}>
              You cancelled all unfinished work at {time(status?.last_kill_at)}. The brain and its{' '}
              {count(facts.length, 'fact')} are intact.
            </p>
            <p className="faint" style={{ fontSize: 13 }}>
              To start again, lift the pause, then give the Chief of Staff a new order.
            </p>
            <button className="btn pri" onClick={() => void resume()} disabled={busy} style={{ marginTop: 6 }}>
              <Icon name="play" />
              Lift the pause
            </button>
          </div>
        </div>
      ) : null}
    </main>
  );
}
