'use client';

/**
 * The Brain's filters (Main.dc.html's header): a department, a time window,
 * and only what needs the owner. They dim what is outside them; nothing is
 * hidden, and the ring opens the department filtered to.
 */

import { departmentName } from '@/lib/format';
import type { Department } from '@/lib/types';

export type Window = 'all' | 'today' | 'hour';

export interface BrainFilter {
  department: string | null;
  window: Window;
  needsOnly: boolean;
}

export const NO_FILTER: BrainFilter = { department: null, window: 'all', needsOnly: false };

const WINDOWS: [Window, string][] = [
  ['all', 'All time'],
  ['today', 'Today'],
  ['hour', 'The last hour'],
];

export function Filters({
  filter,
  onChange,
  departments,
  waiting,
}: {
  filter: BrainFilter;
  onChange: (next: BrainFilter) => void;
  departments: Department[];
  waiting: number;
}) {
  return (
    <div className="filters" role="group" aria-label="Filters">
      <label className="glass select">
        <span className="vh">Department</span>
        <select
          value={filter.department ?? ''}
          onChange={(e) => onChange({ ...filter, department: e.target.value || null })}
        >
          <option value="">All departments</option>
          {departments.map((d) => (
            <option key={d.id} value={d.id}>
              {departmentName(d.name)}
            </option>
          ))}
        </select>
      </label>
      <label className="glass select">
        <span className="vh">Time</span>
        <select value={filter.window} onChange={(e) => onChange({ ...filter, window: e.target.value as Window })}>
          {WINDOWS.map(([value, words]) => (
            <option key={value} value={value}>
              {words}
            </option>
          ))}
        </select>
      </label>
      <button
        className="btn sm glass"
        role="switch"
        aria-checked={filter.needsOnly}
        onClick={() => onChange({ ...filter, needsOnly: !filter.needsOnly })}
      >
        <span className="sw" aria-hidden="true" />
        Only what needs me
        {waiting ? (
          <span className="num" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color: 'var(--ice)' }}>
            <span className="gl wait" aria-hidden="true" />
            {waiting}
          </span>
        ) : null}
      </button>
    </div>
  );
}

/** When the time window starts, or null for all time. */
export function windowStart(window: Window, dayStartsAt: string | undefined): number | null {
  if (window === 'hour') return Date.now() - 3600_000;
  if (window === 'today') return dayStartsAt ? Date.parse(dayStartsAt) : Date.now() - 86_400_000;
  return null;
}
