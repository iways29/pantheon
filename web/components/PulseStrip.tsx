'use client';

import { Icon } from '@/components/Icon';
import { isKilled, useCompany } from '@/lib/company';
import { money, time } from '@/lib/format';

/** Today's pulse: spend against budget, facts, tasks, what waits for the owner. */
export function PulseStrip() {
  const { pulse, status, live } = useCompany();
  const spend = pulse?.spend_usd ?? 0;
  const budget = pulse?.budget_usd ?? 0;
  const share = budget > 0 ? Math.min(100, (spend / budget) * 100) : 0;
  const over = budget > 0 && spend > budget;
  const waiting = pulse?.needs_you_total ?? 0;

  const paused = status?.state === 'paused';
  const liveMark = paused ? (isKilled(status) ? 'gl stop' : 'gl pause') : live === 'live' ? 'run-dot' : 'run-dot quiet';
  const liveWords = paused
    ? 'Held at the last event'
    : live === 'live'
      ? 'Live'
      : live === 'connecting'
        ? 'Connecting'
        : 'Offline, reconnecting';

  return (
    <footer className="pulse" aria-label="Today’s pulse">
      <span className="disp" style={{ fontSize: 16 }}>
        Today
      </span>
      <span className="pulse-item" title={pulse ? `Since ${time(pulse.day_starts_at)}, the budget day` : ''}>
        <span className="faint">Spend</span>
        <span className="num strong">{money(spend)}</span>
        <span className="faint num">of {money(budget)}</span>
        <span
          className={`bar${over ? ' over' : ''}`}
          role="img"
          aria-label={over ? 'Over budget' : `${Math.round(share)}% of today’s budget`}
        >
          <span style={{ width: `${over ? 100 : share}%` }} />
        </span>
      </span>
      <span className="pulse-item">
        <span className="faint">Facts</span>
        <span className="num strong">+{pulse?.facts_added ?? 0} added</span>
        <span className="faint num">{pulse?.facts_rejected ?? 0} rejected</span>
      </span>
      <span className="pulse-item">
        <span className="faint">Tasks</span>
        <span className="num strong">{pulse?.tasks_done ?? 0} done</span>
        {pulse?.tasks_failed ? (
          <span className="faint num">{pulse.tasks_failed} failed</span>
        ) : null}
      </span>
      <span className="pulse-item">
        <span className="faint">Waiting for you</span>
        <span className="gl wait" aria-hidden="true" />
        <span className="num strong" style={{ color: 'var(--ice)' }}>
          {waiting}
        </span>
      </span>
      <span style={{ flexGrow: 1 }} />
      <span className="pulse-item muted">
        <span className={liveMark} aria-hidden="true" />
        {liveWords}
      </span>
      {/* Main.dc.html: the day, played again on the brain. */}
      <button
        type="button"
        className="btn sm glass replay-btn"
        onClick={() => window.dispatchEvent(new CustomEvent('pantheon:replay'))}
      >
        <Icon name="replay" size={14} />
        Replay the day
      </button>
    </footer>
  );
}
