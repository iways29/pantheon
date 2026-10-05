'use client';

import { useEffect, useState } from 'react';

import { useCompany } from '@/lib/company';

/**
 * What shows while Pantheon wakes: the mark, its hand going round, and the
 * name. The first load after a quiet spell starts the API from cold, which
 * takes a few seconds; this stands in front of an empty screen until the
 * company is loaded, then fades away.
 */
export function Splash({ done = false, note }: { done?: boolean; note?: string }) {
  const [gone, setGone] = useState(false);
  // A slow wake says so, rather than looking stuck.
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!done) {
      const timer = setTimeout(() => setSlow(true), 4000);
      return () => clearTimeout(timer);
    }
    const timer = setTimeout(() => setGone(true), 450);
    return () => clearTimeout(timer);
  }, [done]);
  if (gone) return null;
  return (
    <div className={`splash${done ? ' out' : ''}`} role="status" aria-live="polite" aria-busy={!done}>
      <svg className="splash-mark" width="64" height="64" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="10" />
        <circle cx="12" cy="12" r="5.5" />
        <path d="M12 2v3" />
        <path className="splash-hand" d="M12 12l4.5-5.5" />
      </svg>
      <span className="disp splash-name">Pantheon</span>
      <span className="faint splash-note">{note ?? (slow ? 'Waking the brain, nearly there' : 'Waking the brain')}</span>
    </div>
  );
}

/** The splash over the signed-in screens, until the company has loaded (or
 * failed to: the screen then says why). */
export function CompanySplash() {
  const { snapshot, error, preview } = useCompany();
  if (preview) return null;
  return <Splash done={snapshot !== null || error !== null} />;
}
