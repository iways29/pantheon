'use client';

/**
 * The instrument around the globe (Field.dc.html): a 24-hour dial with the
 * hour now and a tick for each moment of today's work, and the agents on a
 * ring outside it, grouped by department, each wearing its state's shape.
 * When an agent reads the brain a thread runs out to it; when it writes, in.
 */

import type { Agent, Department } from '@/lib/types';
import { agentName, departmentName, OWNER_TIME_ZONE } from '@/lib/format';

export interface Thread {
  key: string;
  agentId: string;
  /** in: the agent writes to the brain; out: it reads from it. */
  way: 'in' | 'out';
}

interface Props {
  width: number;
  height: number;
  /** The globe's radius on screen at rest. */
  radius: number;
  agents: Agent[];
  departments: Department[];
  /** Times of today's events (ISO), for the dial's ticks. */
  moments: string[];
  threads: Thread[];
  selected: string | null;
  onSelect: (agentName: string) => void;
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

/** Where each agent sits: departments share the ring, the executive on top. */
export function ringLayout(agents: Agent[], departments: Department[]) {
  const order = [...departments].sort((a, b) =>
    a.name === 'executive' ? -1 : b.name === 'executive' ? 1 : a.name.localeCompare(b.name),
  );
  const groups = order
    .map((d) => ({
      department: d,
      agents: agents
        .filter((a) => a.department_id === d.id)
        .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name)),
    }))
    .filter((g) => g.agents.length);
  const slots = groups.reduce((n, g) => n + g.agents.length + 1, 0) || 1;
  const step = (Math.PI * 2) / slots;
  let cursor = -Math.PI / 2 - ((groups[0]?.agents.length ?? 1) - 1) * step * 0.5;
  const placed = new Map<string, { angle: number; group: number }>();
  const arcs: { name: string; from: number; to: number; mid: number }[] = [];
  groups.forEach((g, gi) => {
    const from = cursor;
    // The head in the middle of its department's arc.
    const seats = middleOut(g.agents);
    seats.forEach((agent, i) => placed.set(agent.id, { angle: from + i * step, group: gi }));
    const to = from + (g.agents.length - 1) * step;
    arcs.push({ name: g.department.name, from, to, mid: (from + to) / 2 });
    cursor = to + step * 2;
  });
  return { placed, arcs };
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

export function Dial({
  width,
  height,
  radius,
  agents,
  departments,
  moments,
  threads,
  selected,
  onSelect,
}: Props) {
  if (!width || !height || !radius) return null;
  const cx = width / 2;
  const cy = height / 2;
  const dialR = radius * 1.14;
  const ringR = radius * 1.36;
  const now = minutes(new Date());
  const { placed, arcs } = ringLayout(agents, departments);

  // Today's work, in ten-minute buckets: a longer tick for a busier moment.
  const buckets = new Map<number, number>();
  for (const iso of moments) {
    const bucket = Math.floor(minutes(new Date(iso)) / 10);
    buckets.set(bucket, (buckets.get(bucket) ?? 0) + 1);
  }

  const hand = at(cx, cy, dialR + 10, angleOf(now));
  const handIn = at(cx, cy, dialR - 12, angleOf(now));
  const handLabel = at(cx, cy, dialR + 24, angleOf(now));
  const clock = `${String(Math.floor(now / 60)).padStart(2, '0')}:${String(now % 60).padStart(2, '0')}`;

  return (
    <svg className="dial" width={width} height={height} aria-hidden={false} role="group" aria-label="Agents around the brain">
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
      {/* Today's work. */}
      {[...buckets].map(([bucket, n]) => {
        const a = angleOf(bucket * 10 + 5);
        const [x1, y1] = at(cx, cy, dialR + 3, a);
        const [x2, y2] = at(cx, cy, dialR + 3 + Math.min(12, 3 + n * 1.5), a);
        return <line key={bucket} x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--gold)" strokeOpacity={0.55} strokeWidth={1.5} />;
      })}
      <line x1={handIn[0]} y1={handIn[1]} x2={hand[0]} y2={hand[1]} stroke="var(--gold)" strokeWidth={2} strokeLinecap="round" />
      <text x={handLabel[0]} y={handLabel[1]} className="dial-time" textAnchor="middle" dominantBaseline="middle">
        {clock}
      </text>

      {/* Departments. */}
      {arcs.map((arc) => {
        const pad = 0.12;
        const [x1, y1] = at(cx, cy, ringR, arc.from - pad);
        const [x2, y2] = at(cx, cy, ringR, arc.to + pad);
        const large = arc.to - arc.from + 2 * pad > Math.PI ? 1 : 0;
        return (
          <path
            key={arc.name}
            d={`M ${x1} ${y1} A ${ringR} ${ringR} 0 ${large} 1 ${x2} ${y2}`}
            fill="none"
            stroke="var(--line)"
            strokeWidth={1.2}
          />
        );
      })}

      {/* Threads between the brain and an agent. */}
      {threads.map((t) => {
        const seat = placed.get(t.agentId);
        if (!seat) return null;
        const [x, y] = at(cx, cy, ringR, seat.angle);
        const [ex, ey] = at(cx, cy, radius * 0.2, seat.angle);
        const [from, to] = t.way === 'in' ? [[x, y], [ex, ey]] : [[ex, ey], [x, y]];
        return (
          <g key={t.key} className={`thread ${t.way}`}>
            <line x1={from![0]} y1={from![1]} x2={to![0]} y2={to![1]} className="thread-path" />
            <line
              x1={from![0]}
              y1={from![1]}
              x2={to![0]}
              y2={to![1]}
              pathLength={100}
              className="thread-bead"
            />
          </g>
        );
      })}

      {/* The agents. */}
      {agents.map((agent) => {
        const seat = placed.get(agent.id);
        if (!seat) return null;
        const [x, y] = at(cx, cy, ringR, seat.angle);
        const [lx, ly] = at(cx, cy, ringR + 18, seat.angle);
        const right = Math.cos(seat.angle) > 0.2;
        const left = Math.cos(seat.angle) < -0.2;
        const lead = agent.role !== 'worker';
        return (
          <g
            key={agent.id}
            className={`seat${selected === agent.name ? ' picked' : ''}`}
            role="button"
            tabIndex={0}
            aria-label={`${agentName(agent.name)}, ${agent.state}`}
            onClick={() => onSelect(agent.name)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onSelect(agent.name);
              }
            }}
          >
            <circle cx={x} cy={y} r={16} className="seat-hit" />
            {lead ? <circle cx={x} cy={y} r={11} fill="none" stroke="var(--ink3)" /> : null}
            <SeatMark x={x} y={y} state={agent.state} />
            <text
              x={lx}
              y={ly}
              className="seat-name"
              textAnchor={right ? 'start' : left ? 'end' : 'middle'}
              dominantBaseline="middle"
            >
              {agentName(agent.name)}
            </text>
            {lead ? (
              <text
                x={lx}
                y={ly + 13}
                className="seat-role"
                textAnchor={right ? 'start' : left ? 'end' : 'middle'}
                dominantBaseline="middle"
              >
                {agent.role === 'chief_of_staff' ? 'Chief of Staff' : `${departmentName(agent.department)} head`}
              </text>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
}

/** The state's shape, in SVG: never colour alone. */
function SeatMark({ x, y, state }: { x: number; y: number; state: Agent['state'] }) {
  switch (state) {
    case 'working':
      return (
        <>
          <circle cx={x} cy={y} r={9} fill="var(--goldS)" />
          <circle cx={x} cy={y} r={5.5} fill="var(--gold)" />
        </>
      );
    case 'waiting':
      return (
        <rect
          x={x - 5}
          y={y - 5}
          width={10}
          height={10}
          rx={1.5}
          transform={`rotate(45 ${x} ${y})`}
          fill="var(--iceS)"
          stroke="var(--ice)"
          strokeWidth={1.5}
        />
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
      return <circle cx={x} cy={y} r={5} fill="var(--bg)" stroke="var(--ink2)" strokeWidth={1.5} />;
  }
}
