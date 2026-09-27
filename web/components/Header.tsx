'use client';

import Link from 'next/link';
import { useState, type ReactNode } from 'react';

import { DisplayMenu } from '@/components/DisplayMenu';
import { Icon, Logo } from '@/components/Icon';
import { KillDialog } from '@/components/KillDialog';
import { isKilled, useCompany } from '@/lib/company';
import { time } from '@/lib/format';

/**
 * The top bar on every screen: the two tabs, the company's state at a glance,
 * and the controls that must always be in reach: Pause and Kill (ADR 023).
 */
export function Header({ tab, center }: { tab: 'brain' | 'control'; center?: ReactNode }) {
  const { status, agents, setPause } = useCompany();
  const [killing, setKilling] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const killed = isKilled(status);
  const paused = status?.state === 'paused';
  const working = agents.filter((a) => a.state === 'working').length;
  const waiting = agents.filter((a) => a.state === 'waiting').length;

  let pill: { mark: ReactNode; title: string; detail: string };
  if (!status) {
    pill = { mark: <span className="run-dot quiet" />, title: 'Connecting', detail: '' };
  } else if (killed) {
    pill = {
      mark: <span className="gl stop" />,
      title: 'Stopped',
      detail: `at ${time(status.last_kill_at)}`,
    };
  } else if (paused) {
    pill = {
      mark: <span className="gl pause" />,
      title: 'Paused',
      detail: killing ? 'while you decide' : status.since ? `since ${time(status.since)}` : '',
    };
  } else {
    const parts = [working ? `${working} at work` : '', waiting ? `${waiting} waiting` : ''];
    pill = {
      mark: <span className={working ? 'run-dot' : 'run-dot quiet'} />,
      title: 'Running',
      detail: parts.filter(Boolean).join(', ') || 'all agents idle',
    };
  }

  async function act(work: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function startKill() {
    // A kill first pauses everything, so nothing moves while the owner decides.
    void act(async () => {
      if (!paused) await setPause(true);
      setKilling(true);
    });
  }

  return (
    <header className="topbar">
      <div className="brand">
        <Logo />
        <span className="disp" style={{ fontSize: 20 }}>
          Pantheon
        </span>
      </div>
      <nav className="glass tabs" aria-label="Main">
        <Link className="tab" href="/" aria-current={tab === 'brain' ? 'page' : undefined}>
          The brain
        </Link>
        <Link
          className="tab"
          href="/control"
          aria-current={tab === 'control' ? 'page' : undefined}
        >
          Control Center
        </Link>
      </nav>
      <div className="topbar-center">{center}</div>
      <div className="controls">
        <div className="glass pill" role="status" aria-live="polite">
          <span aria-hidden="true" style={{ display: 'inline-flex' }}>
            {pill.mark}
          </span>
          <span className="pill-title">{pill.title}</span>
          <span className="faint pill-detail">{pill.detail}</span>
        </div>
        {paused && !killed ? (
          <button
            className="btn pri"
            onClick={() => void act(() => setPause(false))}
            disabled={busy}
          >
            <Icon name="play" />
            Resume
          </button>
        ) : null}
        {!paused ? (
          <button
            className="btn glass"
            onClick={() => void act(() => setPause(true))}
            disabled={busy || !status}
          >
            <Icon name="pause" />
            Pause
          </button>
        ) : null}
        <button
          className="btn kill"
          onClick={startKill}
          disabled={busy || killed || !status}
          title={killed ? 'All work is already stopped' : undefined}
        >
          <Icon name="kill" />
          Kill
        </button>
        <DisplayMenu />
      </div>
      {error ? (
        <p role="alert" className="topbar-error">
          {error}
        </p>
      ) : null}
      {killing ? <KillDialog onClose={() => setKilling(false)} /> : null}
    </header>
  );
}
