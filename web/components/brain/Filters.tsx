'use client';

/**
 * The Brain's filters (Main.dc.html's header): a department, a time window,
 * and only what needs the owner. They dim what is outside them; nothing is
 * hidden, and the ring opens the department filtered to.
 */

import { useEffect, useRef, useState } from 'react';

import { Icon } from '@/components/Icon';
import { departmentName } from '@/lib/format';
import { useMedia } from '@/lib/useMedia';
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

interface Props {
  filter: BrainFilter;
  onChange: (next: BrainFilter) => void;
  departments: Department[];
  waiting: number;
}

/** Inline on a wide screen; one Filters button when the top bar is tight. */
export function Filters(props: Props) {
  const tight = useMedia('(max-width: 1500px)');
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const { filter } = props;
  const active = (filter.department ? 1 : 0) + (filter.window !== 'all' ? 1 : 0) + (filter.needsOnly ? 1 : 0);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('mousedown', close);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', close);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);

  if (!tight) return <Controls {...props} />;
  return (
    <div ref={box} style={{ position: 'relative' }}>
      <button className="btn sm glass" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        Filters
        {active ? <span className="num" style={{ color: 'var(--ice)' }}>{active}</span> : null}
        {props.waiting ? (
          <span className="num" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--ice)' }}>
            <span className="gl wait" aria-hidden="true" />
            {props.waiting}
          </span>
        ) : null}
        <Icon name="chevron" size={14} />
      </button>
      {open ? (
        <div className="glass popover filters-pop">
          <Controls {...props} stacked />
        </div>
      ) : null}
    </div>
  );
}

function Controls({ filter, onChange, departments, waiting, stacked = false }: Props & { stacked?: boolean }) {
  return (
    <div className={`filters${stacked ? ' stacked' : ''}`} role="group" aria-label="Filters">
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
        <Icon name="chevron" size={14} style={{ position: 'absolute', right: 12, pointerEvents: 'none' }} />
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
        <Icon name="chevron" size={14} style={{ position: 'absolute', right: 12, pointerEvents: 'none' }} />
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
