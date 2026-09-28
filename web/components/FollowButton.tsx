'use client';

import { Icon } from '@/components/Icon';

/**
 * "Follow this order in the brain" (Main.dc.html): the brain draws the
 * order's hand-offs and the facts it touched, and turns to them once.
 */
export function FollowButton({ id, title, short = false }: { id: string; title: string; short?: boolean }) {
  return (
    <button
      type="button"
      className="chip follow-btn"
      aria-label={short ? `Follow “${title}” in the brain` : undefined}
      onClick={() => window.dispatchEvent(new CustomEvent('pantheon:follow', { detail: { id, title } }))}
    >
      <Icon name="path" size={13} />
      {short ? 'Follow' : 'Follow this order in the brain'}
    </button>
  );
}
