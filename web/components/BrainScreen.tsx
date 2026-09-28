'use client';

import { useEffect, useState } from 'react';

import { ApprovalCard } from '@/components/ApprovalCard';
import { BrainStage } from '@/components/BrainStage';
import { Chat, type PanelMode } from '@/components/Chat';
import { type BrainFilter, Filters, NO_FILTER } from '@/components/brain/Filters';
import { AGENT_MARK, Mark } from '@/components/Mark';
import { Header } from '@/components/Header';
import { Icon, Logo } from '@/components/Icon';
import { KillDialog } from '@/components/KillDialog';
import { NeedsYou } from '@/components/NeedsYou';
import { OrderPath } from '@/components/OrderPath';
import { PulseStrip } from '@/components/PulseStrip';
import { isKilled, useCompany } from '@/lib/company';
import { money, time } from '@/lib/format';
import type { Approval } from '@/lib/types';
import { useMedia } from '@/lib/useMedia';

/** Phone widths get separate screens with a tab bar (the phone designs). */
const PHONE = '(max-width: 760px)';

/** The Brain tab: what needs the owner, the brain, the chat, today's pulse. */
export function BrainScreen() {
  const phone = useMedia(PHONE);
  const [open, setOpen] = useState<Approval | null>(null);
  const { approvals } = useCompany();
  // A work card's "Open" asks for its approval by id.
  useEffect(() => {
    const onAsk = (event: Event) => {
      const id = (event as CustomEvent<string>).detail;
      const found = approvals.find((a) => a.id === id);
      if (found) setOpen(found);
    };
    window.addEventListener('pantheon:approval', onAsk);
    return () => window.removeEventListener('pantheon:approval', onAsk);
  }, [approvals]);
  const card = open ? <ApprovalCard approval={open} onClose={() => setOpen(null)} /> : null;
  return phone ? (
    <PhoneScreen onOpen={setOpen} card={card} />
  ) : (
    <DeskScreen onOpen={setOpen} onCloseCard={() => setOpen(null)} card={card} />
  );
}

function DeskScreen({
  onOpen,
  onCloseCard,
  card,
}: {
  onOpen: (a: Approval) => void;
  onCloseCard: () => void;
  card: React.ReactNode;
}) {
  const { error, departments, pulse, agents } = useCompany();
  const [filter, setFilter] = useState<BrainFilter>(NO_FILTER);
  const [chat, setChat] = useState<PanelMode>('open');
  const [needs, setNeeds] = useState<'open' | 'min'>('open');
  const waiting = pulse?.needs_you_total ?? 0;
  const chief = agents.find((a) => a.role === 'chief_of_staff');
  // Folding Needs you closes the card it opened.
  function foldNeeds() {
    setNeeds('min');
    onCloseCard();
  }
  const layout = `body${needs === 'min' ? ' no-needs' : ''}${chat === 'min' ? ' no-chat' : ''}${chat === 'max' ? ' chat-max' : ''}`;

  return (
    <div className="screen">
      <Header
        tab="brain"
        center={<Filters filter={filter} onChange={setFilter} departments={departments} waiting={waiting} />}
      />
      <div className={layout}>
        {/* Folded panels stay mounted (hidden), so opening them again is instant. */}
        <aside
          className="glass panel needs-panel"
          aria-label="Needs you"
          hidden={needs !== 'open' || chat === 'max'}
          style={{ padding: '18px 16px' }}
        >
          <NeedsYou onOpen={onOpen} onCollapse={foldNeeds} />
          <OrderPath />
          {error ? (
            <p role="alert" style={{ marginTop: 'auto', color: 'var(--verm)', fontSize: 13 }}>
              {error}
            </p>
          ) : null}
        </aside>
        {chat !== 'max' ? <BrainStage filter={filter} /> : null}
        <div className="chat-slot" hidden={chat === 'min'}>
          <Chat mode={chat} onMode={setChat} />
        </div>
      </div>
      <PulseStrip />
      {needs === 'min' ? (
        <button className="glass bubble bubble-left" onClick={() => setNeeds('open')} aria-label={`Needs you, ${waiting} waiting. Open`}>
          <span className="gl wait" aria-hidden="true" />
          Needs you
          {waiting ? <span className="num bubble-count">{waiting}</span> : null}
        </button>
      ) : null}
      {chat === 'min' ? (
        <button className="glass bubble bubble-right" onClick={() => setChat('open')} aria-label="Open the chat">
          {chief ? <Mark kind={AGENT_MARK[chief.state]} /> : <Icon name="chat" size={16} />}
          Chat
        </button>
      ) : null}
      {card}
    </div>
  );
}

type Tab = 'brain' | 'needs' | 'chat';

function PhoneScreen({ onOpen, card }: { onOpen: (a: Approval) => void; card: React.ReactNode }) {
  const { pulse } = useCompany();
  const [tab, setTab] = useState<Tab>('brain');
  const waiting = pulse?.needs_you_total ?? 0;
  const tabs: [Tab, string, React.ReactNode][] = [
    ['brain', 'Brain', <Logo key="b" size={18} />],
    ['needs', waiting ? `Needs you, ${waiting}` : 'Needs you', <span key="n" className="gl wait" aria-hidden="true" />],
    ['chat', 'Chat', <Icon key="c" name="chat" size={18} />],
  ];

  return (
    <div className="phone">
      <PhoneTop />
      <div className="phone-view">
        {tab === 'brain' ? (
          <>
            <div className="phone-globe">
              <BrainStage />
            </div>
            <PhonePulse />
            <div className="glass phone-card">
              <NeedsYou onOpen={onOpen} limit={3} onMore={() => setTab('needs')} />
            </div>
          </>
        ) : null}
        {tab === 'needs' ? (
          <div className="glass phone-card" style={{ minHeight: '60vh' }}>
            <NeedsYou onOpen={onOpen} />
          </div>
        ) : null}
        {tab === 'chat' ? <Chat /> : null}
      </div>
      <nav className="glass phone-tabs" aria-label="Screens">
        {tabs.map(([key, label, mark]) => (
          <button key={key} aria-current={tab === key ? 'page' : undefined} onClick={() => setTab(key)}>
            {mark}
            <span>{label}</span>
          </button>
        ))}
      </nav>
      {card}
    </div>
  );
}

/** The phone's top: the state at a glance, and Pause and Kill always in reach. */
function PhoneTop() {
  const { status, setPause } = useCompany();
  const [killing, setKilling] = useState(false);
  const [busy, setBusy] = useState(false);
  const killed = isKilled(status);
  const paused = status?.state === 'paused';

  async function act(work: () => Promise<void>) {
    setBusy(true);
    try {
      await work();
    } finally {
      setBusy(false);
    }
  }

  return (
    <header className="phone-top">
      <div className="row-between" style={{ alignItems: 'center' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Logo size={22} />
          <span className="disp" style={{ fontSize: 19 }}>
            Pantheon
          </span>
        </span>
        <span className="glass pill" role="status" style={{ height: 34 }}>
          <span className={killed ? 'gl stop' : paused ? 'gl pause' : 'run-dot'} aria-hidden="true" />
          <span className="pill-title" style={{ fontSize: 13 }}>
            {killed ? `Stopped ${time(status?.last_kill_at)}` : paused ? 'Paused' : 'Running'}
          </span>
        </span>
      </div>
      <div className="two">
        {paused ? (
          <button className="btn pri" disabled={busy} onClick={() => void act(() => setPause(false))}>
            <Icon name="play" />
            Resume
          </button>
        ) : (
          <button className="btn glass" disabled={busy || !status} onClick={() => void act(() => setPause(true))}>
            <Icon name="pause" />
            Pause
          </button>
        )}
        <button
          className="btn kill"
          disabled={busy || killed || !status}
          onClick={() =>
            void act(async () => {
              if (!paused) await setPause(true);
              setKilling(true);
            })
          }
        >
          <Icon name="kill" />
          Kill
        </button>
      </div>
      {killing ? <KillDialog onClose={() => setKilling(false)} /> : null}
    </header>
  );
}

/** Today's pulse as a card (Phone.dc.html). */
function PhonePulse() {
  const { pulse } = useCompany();
  const spend = pulse?.spend_usd ?? 0;
  const budget = pulse?.budget_usd ?? 0;
  const share = budget > 0 ? Math.min(100, (spend / budget) * 100) : 0;
  return (
    <div className="glass phone-card phone-pulse" aria-label="Today’s pulse">
      <div>
        <span className="faint">Spend today</span>
        <span className="num big">
          {money(spend)} <span className="faint small">of {money(budget)}</span>
        </span>
        <span className={`bar${budget > 0 && spend > budget ? ' over' : ''}`} style={{ width: '100%' }}>
          <span style={{ width: `${share}%` }} />
        </span>
      </div>
      <div>
        <span className="faint">Facts</span>
        <span className="num big">
          +{pulse?.facts_added ?? 0} <span className="faint small">{pulse?.facts_rejected ?? 0} rejected</span>
        </span>
      </div>
      <div>
        <span className="faint">Tasks</span>
        <span className="num big">{pulse?.tasks_done ?? 0} done</span>
      </div>
      <div>
        <span className="faint">Waiting for you</span>
        <span className="num big" style={{ color: 'var(--ice)' }}>
          <span className="gl wait" aria-hidden="true" /> {pulse?.needs_you_total ?? 0}
        </span>
      </div>
    </div>
  );
}
