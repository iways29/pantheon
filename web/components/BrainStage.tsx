'use client';

import { useState } from 'react';

import { Icon } from '@/components/Icon';
import { FACT_MARK, Mark } from '@/components/Mark';
import { isKilled, useCompany } from '@/lib/company';
import { count, time } from '@/lib/format';
import type { MapFact } from '@/lib/types';

/**
 * The brain's stage: title, counts, legends, and the paused and stopped
 * veils. The map itself is drawn plainly here (SVG, the real positions);
 * Phase 3 replaces it with the PixiJS lens, dial and agent ring.
 */
export function BrainStage() {
  const { snapshot, agents, departments, status, pulse, setPause } = useCompany();
  const [busy, setBusy] = useState(false);
  const facts = snapshot?.facts ?? [];
  const held = snapshot?.held ?? [];
  const hoods = snapshot?.neighbourhoods ?? [];
  const byStatus = (s: MapFact['status']) => facts.filter((f) => f.status === s).length;
  const killed = isKilled(status);
  const paused = status?.state === 'paused' && !killed;

  async function resume() {
    setBusy(true);
    try {
      await setPause(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="stage" aria-label="The brain">
      <MapPreview />
      <div style={{ position: 'absolute', left: 16, top: 12, display: 'grid', gap: 2, pointerEvents: 'none' }}>
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

      <div className="glass legend" style={{ left: 16, gridTemplateColumns: '12px auto auto' }}>
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

      <div className="glass legend" style={{ right: 16, gridTemplateColumns: '12px auto' }}>
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
      </div>

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

/** The map, drawn plainly: every placed fact at its place, by status. */
function MapPreview() {
  const { snapshot } = useCompany();
  const facts = (snapshot?.facts ?? []).filter((f) => f.x !== null && f.y !== null);
  const hoods = snapshot?.neighbourhoods ?? [];
  const R = 300;
  const at = (v: number) => Math.round(v * R * 0.94 * 100) / 100;

  return (
    <svg
      viewBox={`${-R - 40} ${-R - 40} ${2 * R + 80} ${2 * R + 80}`}
      style={{ position: 'absolute', inset: '56px 0 0', width: '100%', height: 'calc(100% - 56px)' }}
      role="img"
      aria-label={`Map of ${facts.length} facts in ${hoods.length} neighbourhoods`}
    >
      <circle r={R} fill="var(--glass)" stroke="var(--edge)" />
      <circle r={R + 18} fill="none" stroke="var(--line)" strokeDasharray="1 9" />
      {facts.map((f) =>
        f.status === 'superseded' ? (
          <circle key={f.id} cx={at(f.x!)} cy={at(f.y!)} r={3.5} fill="none" stroke="var(--ink3)" />
        ) : f.status === 'disputed' ? (
          <g key={f.id}>
            <circle cx={at(f.x!)} cy={at(f.y!)} r={3} fill="var(--verm)" />
            <circle cx={at(f.x!)} cy={at(f.y!)} r={6} fill="none" stroke="var(--verm)" strokeDasharray="2 2" />
          </g>
        ) : (
          <circle key={f.id} cx={at(f.x!)} cy={at(f.y!)} r={3.5} fill="var(--gold)">
            <title>{f.claim}</title>
          </circle>
        ),
      )}
      {hoods.map((h) => (
        <text
          key={h.id}
          x={at(h.x)}
          y={at(h.y) - 12}
          textAnchor="middle"
          fill="var(--ink2)"
          style={{ font: '400 13px var(--font-display), serif' }}
        >
          {h.label}
        </text>
      ))}
    </svg>
  );
}
