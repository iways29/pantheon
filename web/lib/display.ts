'use client';

/**
 * The owner's display settings, kept in this browser only (a convenience, not
 * company data): theme, motion and glass. Applied to <html> before first paint
 * by the script in app/layout.tsx.
 */

import { useCallback, useEffect, useState } from 'react';

export interface Display {
  theme: 'dark' | 'light';
  motion: 'full' | 'reduced';
  glass: 'glass' | 'solid';
}

const KEY = 'pantheon.display';
const DEFAULTS: Display = { theme: 'dark', motion: 'full', glass: 'glass' };

function read(): Display {
  try {
    return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(KEY) || '{}') as Partial<Display>) };
  } catch {
    return DEFAULTS;
  }
}

function apply(display: Display): void {
  const root = document.documentElement;
  root.dataset.theme = display.theme;
  root.classList.toggle('rm', display.motion === 'reduced');
  root.classList.toggle('solid', display.glass === 'solid');
}

export function useDisplay(): [Display, (change: Partial<Display>) => void] {
  const [display, setDisplay] = useState<Display>(DEFAULTS);

  useEffect(() => {
    setDisplay(read());
  }, []);

  const update = useCallback((change: Partial<Display>) => {
    setDisplay((prev) => {
      const next = { ...prev, ...change };
      apply(next);
      try {
        localStorage.setItem(KEY, JSON.stringify(next));
      } catch {
        // Private windows may refuse storage; the setting still applies now.
      }
      return next;
    });
  }, []);

  return [display, update];
}

/** True when motion should be still: the owner's setting or the system's. */
export function prefersStill(): boolean {
  if (typeof window === 'undefined') return false;
  return (
    document.documentElement.classList.contains('rm') ||
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}
