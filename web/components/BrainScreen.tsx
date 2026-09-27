'use client';

import { BrainStage } from '@/components/BrainStage';
import { Chat } from '@/components/Chat';
import { Header } from '@/components/Header';
import { NeedsYou } from '@/components/NeedsYou';
import { PulseStrip } from '@/components/PulseStrip';
import { useCompany } from '@/lib/company';

/** The Brain tab: what needs the owner, the brain, the chat, today's pulse. */
export function BrainScreen() {
  const { error } = useCompany();
  return (
    <div className="screen">
      <Header tab="brain" />
      <div className="body">
        <aside className="glass panel" aria-label="Needs you" style={{ padding: '18px 16px' }}>
          <NeedsYou />
          {error ? (
            <p role="alert" style={{ marginTop: 'auto', color: 'var(--verm)', fontSize: 13 }}>
              {error}
            </p>
          ) : null}
        </aside>
        <BrainStage />
        <Chat />
      </div>
      <PulseStrip />
    </div>
  );
}
