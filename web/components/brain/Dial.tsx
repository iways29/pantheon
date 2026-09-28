'use client';

/**
 * The instrument around the globe (Field.dc.html, ScaleNotes.dc.html): a
 * 24-hour dial with the hour now and a tick for each moment of today's work,
 * and the agents on a ring outside it.
 *
 * Departments open and close. A closed department is one hub: its agent
 * count, a dot per agent in its state's shape, a gold ring when anyone works
 * and a blue halo when someone needs the owner. Tapping a hub opens it in
 * place; a small close button sits at the start of its arc. The Executive
 * Office and any department filtered to are open; nothing opens or closes by
 * itself. When space runs out, workers lose their labels (heads and anyone
 * waiting keep them); hover or focus shows the name. A thread to an agent in
 * a closed department ends at its hub.
 */

import type { Agent, Department } from '@/lib/types';
import { agentName, departmentName, OWNER_TIME_ZONE } from '@/lib/format';

export interface Thread {
  key: string;
  agentId: string;
  /** in: the agent writes to the brain; out: it reads from it. */
  way: 'in' | 'out';
  /** The fact it wrote or read, when the event names one. */
  factId?: string;
}

export type Locate = (factId: string) => { x: number; y: number; front: boolean } | null;

interface Props {
  width: number;
  height: number;
  /** The globe's radius on screen at rest. */
  radius: number;
  agents: Agent[];
  departments: Department[];
  /** Departments shown open on the ring. */
  open: Set<string>;
  onToggle: (departmentId: string) => void;
  /** Times of today's events (ISO), for the dial's ticks. */
  moments: string[];
  threads: Thread[];
  selected: string | null;
  onSelect: (agentName: string) => void;
  /** Where a fact is on screen now (the globe's own projection). */
  locate?: Locate;
  /** The moment the hand shows while replaying (ms); null or absent: now. */
  handAt?: number | null;
  /** Midnight in New York (ms): minute 0 of the dial. */
  dayStart?: number;
  /** Dragging the hand replays the day from there (Events.dc.html). */
  onScrub?: (at: number) => void;
}

const minutesOf = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
  timeZone: OWNER_TIME_ZONE,
});

function minutes(date: Date): number {
  const [h, m] = minutesOf.format(date).split(':').map(Number);
  return (h ?? 0) * 60 + (m ?? 0);
}

/** 00:00 at the top, clockwise. */
function angleOf(minute: number): number {
  return (minute / 1440) * Math.PI * 2 - Math.PI / 2;
}

function at(cx: number, cy: number, r: number, angle: number): [number, number] {
  return [cx + Math.cos(angle) * r, cy + Math.sin(angle) * r];
}

/** With no fact named, a thread ends just inside the glass, a little along
 * the orbit from its agent, never at the centre. */
function rimSpot(cx: number, cy: number, radius: number, angle: number): { x: number; y: number } {
  const [x, y] = at(cx, cy, radius * 0.82, angle + 0.35);
  return { x, y };
}

/**
 * A thread between an agent on the ring and a point on the globe, drawn as
 * an arc that swings along the orbit on its way in (owner, 2026-09-27: "the
 * calls should orbit in an arch, not a straight line"). It leaves the agent
 * going round the ring, turning in toward the point; `way` sets which end
 * the bead starts from.
 */
export function orbit(
  cx: number,
  cy: number,
  agent: [number, number],
  spot: { x: number; y: number },
  way: 'in' | 'out',
): string {
  const ra = Math.hypot(agent[0] - cx, agent[1] - cy);
  const rf = Math.hypot(spot.x - cx, spot.y - cy);
  const ta = Math.atan2(agent[1] - cy, agent[0] - cx);
  // Near the centre a point has no clear angle: go clockwise.
  const tf = rf < 4 ? ta + 0.6 : Math.atan2(spot.y - cy, spot.x - cx);
  let sweep = Math.atan2(Math.sin(tf - ta), Math.cos(tf - ta));
  if (Math.abs(sweep) < 0.6) sweep = sweep < 0 ? -0.6 : 0.6;
  const [kx, ky] = at(cx, cy, ra * 0.82 + rf * 0.18, ta + sweep * 0.65);
  const [from, to] = way === 'in' ? [agent, [spot.x, spot.y]] : [[spot.x, spot.y], agent];
  const f = (n: number) => n.toFixed(1);
  return `M ${f(from[0]!)} ${f(from[1]!)} Q ${f(kx)} ${f(ky)} ${f(to[0]!)} ${f(to[1]!)}`;
}

function rank(agent: Agent): number {
  return agent.role === 'chief_of_staff' ? 0 : agent.role === 'head' ? 1 : 2;
}

function middleOut<T>(list: T[]): T[] {
  if (list.length < 3) return list;
  const [first, ...rest] = list;
  const half = Math.floor(rest.length / 2);
  return [...rest.slice(0, half), first as T, ...rest.slice(half)];
}

interface Group {
  department: Department;
  agents: Agent[];
  open: boolean;
  /** The hub's angle (closed) or the arc's start and end (open). */
  from: number;
  to: number;
}

/** Where each department and agent sits: the executive on top. */
export function ringLayout(agents: Agent[], departments: Department[], open: Set<string>) {
  const order = [...departments].sort((a, b) =>
    a.name === 'executive' ? -1 : b.name === 'executive' ? 1 : a.name.localeCompare(b.name),
  );
  const groups: Group[] = order
    .map((d) => ({
      department: d,
      agents: middleOut(
        agents
          .filter((a) => a.department_id === d.id)
          .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name)),
      ),
      open: open.has(d.id),
      from: 0,
      to: 0,
    }))
    .filter((g) => g.agents.length);
  // A closed department takes one seat, an open one a seat per agent; a gap
  // of one seat between departments.
  const seats = groups.reduce((n, g) => n + (g.open ? g.agents.length : 1) + 1, 0) || 1;
  const step = (Math.PI * 2) / seats;
  const firstWidth = groups[0] ? (groups[0].open ? groups[0].agents.length : 1) : 1;
  let cursor = -Math.PI / 2 - ((firstWidth - 1) * step) / 2;
  const seat = new Map<string, number>();
  for (const g of groups) {
    g.from = cursor;
    if (g.open) {
      g.agents.forEach((a, i) => seat.set(a.id, cursor + i * step));
      g.to = cursor + (g.agents.length - 1) * step;
    } else {
      g.to = cursor;
    }
    cursor = g.to + step * 2;
  }
  return { groups, seat, step };
}

export function Dial({
  width,
  height,
  radius,
  agents,
  departments,
  open,
  onToggle,
  moments,
  threads,
  selected,
  onSelect,
  locate,
  handAt = null,
  dayStart = 0,
  onScrub,
}: Props) {
  if (!width || !height || !radius) return null;
  const cx = width / 2;
  const cy = height / 2;
  const dialR = radius * 1.14;
  const ringR = radius * 1.36;
  const today = minutes(new Date());
  const now = handAt === null ? today : minutes(new Date(handAt));
  const { groups, seat, step } = ringLayout(agents, departments, open);
  // Workers lose their labels when neighbours sit closer than a label needs.
  const crowded = step * ringR < 46;

  const buckets = new Map<number, number>();
  for (const iso of moments) {
    const bucket = Math.floor(minutes(new Date(iso)) / 10);
    buckets.set(bucket, (buckets.get(bucket) ?? 0) + 1);
  }

  const hand = at(cx, cy, dialR + 10, angleOf(now));
  const handIn = at(cx, cy, dialR - 12, angleOf(now));
  const handLabel = at(cx, cy, dialR + 24, angleOf(now));
  const clock = `${String(Math.floor(now / 60)).padStart(2, '0')}:${String(now % 60).padStart(2, '0')}`;

  /** Where a thread to this agent ends: its seat, or its department's hub. */
  function anchor(agentId: string): number | null {
    const own = seat.get(agentId);
    if (own !== undefined) return own;
    const agent = agents.find((a) => a.id === agentId);
    const g = groups.find((x) => x.department.id === agent?.department_id);
    return g ? g.from : null;
  }

  function label(angle: number) {
    const cos = Math.cos(angle);
    return cos > 0.2 ? 'start' : cos < -0.2 ? 'end' : 'middle';
  }

  return (
    <svg className="dial" width={width} height={height} role="group" aria-label="Agents around the brain">
      {/* The hours. */}
      <circle cx={cx} cy={cy} r={dialR} fill="none" stroke="var(--line)" />
      {Array.from({ length: 96 }, (_, i) => {
        const a = angleOf(i * 15);
        const major = i % 24 === 0;
        const hour = i % 4 === 0;
        const [x1, y1] = at(cx, cy, dialR - (major ? 9 : hour ? 6 : 3), a);
        const [x2, y2] = at(cx, cy, dialR, a);
        return (
          <line
            key={i}
            x1={x1}
            y1={y1}
            x2={x2}
            y2={y2}
            stroke={i * 15 <= now ? 'var(--ink3)' : 'var(--line)'}
            strokeWidth={major ? 1.4 : 1}
          />
        );
      })}
      {[...buckets].map(([bucket, n]) => {
        const a = angleOf(bucket * 10 + 5);
        const [x1, y1] = at(cx, cy, dialR + 3, a);
        const [x2, y2] = at(cx, cy, dialR + 3 + Math.min(12, 3 + n * 1.5), a);
        return (
          <line key={bucket} x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--gold)" strokeOpacity={0.55} strokeWidth={1.5} />
        );
      })}
      <line x1={handIn[0]} y1={handIn[1]} x2={hand[0]} y2={hand[1]} stroke="var(--gold)" strokeWidth={2} strokeLinecap="round" />
      {onScrub ? (
        <circle
          cx={hand[0]}
          cy={hand[1]}
          r={11}
          className="hand-grip"
          role="slider"
          tabIndex={0}
          aria-label="Replay the day: drag the hand"
          aria-valuemin={0}
          aria-valuemax={today}
          aria-valuenow={now}
          aria-valuetext={`${clock}${handAt === null ? ', now' : ''}`}
          onPointerDown={(e) => {
            (e.target as Element).setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (!(e.target as Element).hasPointerCapture(e.pointerId)) return;
            const box = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
            const angle = Math.atan2(e.clientY - box.top - cy, e.clientX - box.left - cx);
            const minute = Math.round(((((angle + Math.PI / 2) / (Math.PI * 2)) * 1440) % 1440 + 1440) % 1440);
            // The future cannot be replayed: past now, the hand stops at now.
            onScrub(dayStart + Math.min(minute, today) * 60000);
          }}
          onKeyDown={(e) => {
            const by = { ArrowLeft: -10, ArrowDown: -10, ArrowRight: 10, ArrowUp: 10, PageDown: -60, PageUp: 60 }[e.key];
            if (by === undefined) return;
            e.preventDefault();
            onScrub(dayStart + Math.max(0, Math.min(now + by, today)) * 60000);
          }}
        />
      ) : null}
      <text x={handLabel[0]} y={handLabel[1]} className="dial-time" textAnchor="middle" dominantBaseline="middle">
        {clock}
      </text>

      {/* Open departments: an arc, and a small close button at its start. */}
      {groups
        .filter((g) => g.open)
        .map((g) => {
          const pad = Math.min(0.12, step * 0.45);
          const [x1, y1] = at(cx, cy, ringR, g.from - pad);
          const [x2, y2] = at(cx, cy, ringR, g.to + pad);
          const large = g.to - g.from + 2 * pad > Math.PI ? 1 : 0;
          const [bx, by] = at(cx, cy, ringR - 22, g.from - pad);
          return (
            <g key={g.department.id}>
              <path d={`M ${x1} ${y1} A ${ringR} ${ringR} 0 ${large} 1 ${x2} ${y2}`} fill="none" stroke="var(--line)" strokeWidth={1.2} />
              {
                <g
                  className="seat hub-close"
                  role="button"
                  tabIndex={0}
                  aria-label={`Close ${departmentName(g.department.name)}`}
                  onClick={() => onToggle(g.department.id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      onToggle(g.department.id);
                    }
                  }}
                >
                  <circle cx={bx} cy={by} r={8} className="hub-close-dot" />
                  <line x1={bx - 3.5} y1={by} x2={bx + 3.5} y2={by} stroke="var(--ink2)" strokeWidth={1.4} />
                </g>
              }
            </g>
          );
        })}

      {/* Threads between the brain and an agent (or its closed hub). */}
      {threads.map((t) => {
        const angle = anchor(t.agentId);
        if (angle === null) return null;
        const spot = t.factId && locate ? locate(t.factId) : null;
        const d = orbit(cx, cy, at(cx, cy, ringR, angle), spot ?? rimSpot(cx, cy, radius, angle), t.way);
        return (
          <g key={t.key} className={`thread ${t.way}${spot && !spot.front ? ' behind' : ''}`}>
            <path d={d} className="thread-path" />
            <path d={d} pathLength={100} className="thread-bead" />
            {spot ? <circle cx={spot.x} cy={spot.y} r={5} className="thread-end" /> : null}
          </g>
        );
      })}

      {/* Closed departments: one hub each. */}
      {groups
        .filter((g) => !g.open)
        .map((g) => {
          const [x, y] = at(cx, cy, ringR, g.from);
          const [lx, ly] = at(cx, cy, ringR + 26, g.from);
          const working = g.agents.filter((a) => a.state === 'working').length;
          const waiting = g.agents.filter((a) => a.state === 'waiting').length;
          const summary = waiting
            ? `${waiting} waiting`
            : working
              ? `${working} at work`
              : g.agents.every((a) => a.state === 'stopped')
                ? 'switched off'
                : 'all idle';
          const name = departmentName(g.department.name);
          return (
            <g
              key={g.department.id}
              className="seat hub"
              role="button"
              tabIndex={0}
              aria-label={`${name}: ${g.agents.length} agents, ${summary}. Open`}
              onClick={() => onToggle(g.department.id)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onToggle(g.department.id);
                }
              }}
            >
              <circle cx={x} cy={y} r={20} className="seat-hit" />
              {waiting ? <circle cx={x} cy={y} r={16} fill="var(--iceS)" className="seat-breathe" /> : null}
              <circle
                cx={x}
                cy={y}
                r={12}
                className={`hub-core${working ? ' busy' : ''}`}
                stroke={working ? 'var(--gold)' : 'var(--ink2)'}
                strokeWidth={1.5}
              />
              <text x={x} y={y} className="hub-count" textAnchor="middle" dominantBaseline="central">
                {g.agents.length}
              </text>
              {g.agents.slice(0, 16).map((a, i) => {
                const angle = (i / Math.min(g.agents.length, 16)) * Math.PI * 2 - Math.PI / 2;
                const [dx, dy] = [x + Math.cos(angle) * 16, y + Math.sin(angle) * 16];
                return <MiniMark key={a.id} x={dx} y={dy} state={a.state} />;
              })}
              <text x={lx} y={ly} className="seat-name" textAnchor={label(g.from)} dominantBaseline="middle">
                {name}
              </text>
              <text x={lx} y={ly + 13} className="seat-role" textAnchor={label(g.from)} dominantBaseline="middle">
                {summary}
              </text>
            </g>
          );
        })}

      {/* Agents of open departments. */}
      {groups
        .filter((g) => g.open)
        .flatMap((g) => g.agents)
        .map((agent) => {
          const angle = seat.get(agent.id);
          if (angle === undefined) return null;
          const [x, y] = at(cx, cy, ringR, angle);
          const [lx, ly] = at(cx, cy, ringR + 18, angle);
          const lead = agent.role !== 'worker';
          const named = !crowded || lead || agent.state === 'waiting';
          const name = agentName(agent.name);
          const role = agent.role === 'chief_of_staff' ? 'Chief of Staff' : lead ? `${departmentName(agent.department)} head` : '';
          return (
            <g
              key={agent.id}
              className={`seat${selected === agent.name ? ' picked' : ''}${named ? '' : ' quiet'}`}
              role="button"
              tabIndex={0}
              aria-label={`${name}, ${agent.state}`}
              onClick={() => onSelect(agent.name)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onSelect(agent.name);
                }
              }}
            >
              <title>{name}</title>
              <circle cx={x} cy={y} r={16} className="seat-hit" />
              {lead ? <circle cx={x} cy={y} r={11} fill="none" stroke="var(--ink3)" strokeOpacity={0.7} /> : null}
              <SeatMark x={x} y={y} state={agent.state} />
              <text x={lx} y={ly} className="seat-name" textAnchor={label(angle)} dominantBaseline="middle">
                {name}
              </text>
              {role && role.toLowerCase() !== name.toLowerCase() ? (
                <text x={lx} y={ly + 13} className="seat-role" textAnchor={label(angle)} dominantBaseline="middle">
                  {role}
                </text>
              ) : null}
            </g>
          );
        })}
    </svg>
  );
}

/** The state's shape, in SVG: never colour alone. At work, a thin gold arc
 * circles the agent; waiting, a blue halo breathes (Field.dc.html). */
function SeatMark({ x, y, state }: { x: number; y: number; state: Agent['state'] }) {
  switch (state) {
    case 'working':
      return (
        <>
          <circle cx={x} cy={y} r={8} fill="var(--goldS)" className="seat-glow" />
          <circle cx={x} cy={y} r={5} fill="var(--gold)" />
          <circle
            cx={x}
            cy={y}
            r={11}
            fill="none"
            stroke="var(--gold)"
            strokeWidth={1.5}
            strokeDasharray="17 52"
            strokeLinecap="round"
            className="seat-spin"
          />
        </>
      );
    case 'waiting':
      return (
        <>
          <circle cx={x} cy={y} r={12} fill="var(--iceS)" className="seat-breathe" />
          <rect x={x - 5} y={y - 5} width={10} height={10} rx={1.5} transform={`rotate(45 ${x} ${y})`} fill="var(--iceS)" stroke="var(--ice)" strokeWidth={1.5} />
        </>
      );
    case 'paused':
      return (
        <>
          <rect x={x - 4.5} y={y - 5} width={3} height={10} fill="var(--ink2)" />
          <rect x={x + 1.5} y={y - 5} width={3} height={10} fill="var(--ink2)" />
        </>
      );
    case 'stopped':
      return <rect x={x - 5} y={y - 5} width={10} height={10} rx={1} fill="none" stroke="var(--ink3)" strokeWidth={1.5} />;
    default:
      return <circle cx={x} cy={y} r={5} fill="none" stroke="var(--ink2)" strokeWidth={1.5} />;
  }
}

/** One agent inside a closed hub: a tiny version of its state's shape. */
function MiniMark({ x, y, state }: { x: number; y: number; state: Agent['state'] }) {
  if (state === 'working') return <circle cx={x} cy={y} r={2.2} fill="var(--gold)" />;
  if (state === 'waiting')
    return <rect x={x - 2} y={y - 2} width={4} height={4} transform={`rotate(45 ${x} ${y})`} fill="none" stroke="var(--ice)" strokeWidth={1.1} />;
  if (state === 'stopped' || state === 'paused')
    return <rect x={x - 1.8} y={y - 1.8} width={3.6} height={3.6} fill="none" stroke="var(--ink3)" strokeWidth={1} />;
  return <circle cx={x} cy={y} r={1.8} fill="none" stroke="var(--ink3)" strokeWidth={1} />;
}
