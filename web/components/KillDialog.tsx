'use client';

import { useEffect, useRef, useState } from 'react';

import { useCompany } from '@/lib/company';
import { count } from '@/lib/format';

/**
 * Kill all unfinished work (ADR 023). Everything is already paused when this
 * opens; the owner types "kill" to confirm, or keeps the pause, or resumes.
 */
export function KillDialog({ onClose }: { onClose: () => void }) {
  const { pulse, snapshot, kill, setPause } = useCompany();
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    input.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const needs = pulse?.needs_you;
  const confirmed = typed.trim().toLowerCase() === 'kill';

  async function act(work: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await work();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <div className="scrim">
      <section
        className="glass dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="kill-h"
        aria-describedby="kill-d"
      >
        <div className="kicker">
          <span className="gl pause" aria-hidden="true" />
          Everything is paused while you decide
        </div>
        <h2 id="kill-h" className="disp" style={{ fontSize: 28 }}>
          Kill all unfinished work?
        </h2>
        <div className="two">
          <div className="g2 box">
            <span className="faint box-h">Cancelled for good</span>
            {count(pulse?.tasks_open ?? 0, 'task')} not finished
            <br />
            {count((needs?.approvals ?? 0) + (needs?.held_facts ?? 0), 'approval')} waiting
            <br />
            {count(needs?.questions ?? 0, 'open question')}
          </div>
          <div className="g2 box">
            <span className="faint box-h">Kept</span>
            {count(snapshot?.facts.length ?? 0, 'fact')} in the brain
            <br />
            Results already delivered
            <br />
            Today’s full event log
          </div>
        </div>
        <p id="kill-d" className="muted" style={{ fontSize: 14, lineHeight: 1.5 }}>
          This can’t be undone. If you only need a break, pause instead.
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (confirmed && !busy) void act(() => kill());
          }}
          style={{ display: 'flex', flexDirection: 'column', gap: 16 }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label htmlFor="kill-in" style={{ fontSize: 13 }}>
              Type <span style={{ fontWeight: 600, color: 'var(--verm)' }}>kill</span> to confirm
            </label>
            <input
              id="kill-in"
              ref={input}
              className="inp"
              autoComplete="off"
              spellCheck={false}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
            />
          </div>
          {error ? (
            <p role="alert" style={{ color: 'var(--verm)', fontSize: 13 }}>
              {error}
            </p>
          ) : null}
          <div className="actions">
            <button type="button" className="btn glass" onClick={onClose} disabled={busy}>
              Keep paused
            </button>
            <button
              type="button"
              className="btn glass"
              onClick={() => void act(() => setPause(false))}
              disabled={busy}
            >
              Resume
            </button>
            <button type="submit" className="btn killfill" disabled={!confirmed || busy}>
              Kill all work
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
