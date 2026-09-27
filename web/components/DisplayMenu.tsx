'use client';

import { useEffect, useRef, useState } from 'react';

import { Icon } from '@/components/Icon';
import { useDisplay, type Display } from '@/lib/display';

const CHOICES: { key: keyof Display; label: string; options: [string, string][] }[] = [
  { key: 'theme', label: 'Theme', options: [['dark', 'Dark'], ['light', 'Light']] },
  { key: 'motion', label: 'Motion', options: [['full', 'Full'], ['reduced', 'Reduced']] },
  { key: 'glass', label: 'Surfaces', options: [['glass', 'Glass'], ['solid', 'Solid']] },
];

/** Display and motion settings, for this browser. */
export function DisplayMenu() {
  const [display, update] = useDisplay();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    window.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div ref={box} style={{ position: 'relative' }}>
      <button
        className="btn ibtn glass"
        aria-label="Display and motion settings"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name="settings" />
      </button>
      {open ? (
        <div className="glass popover" role="group" aria-label="Display and motion">
          {CHOICES.map((choice) => (
            <div key={choice.key} className="popover-row">
              <span className="faint" style={{ fontSize: 12 }}>
                {choice.label}
              </span>
              <div className="seg g2" role="group" aria-label={choice.label}>
                {choice.options.map(([value, words]) => (
                  <button
                    key={value}
                    aria-pressed={display[choice.key] === value}
                    onClick={() => update({ [choice.key]: value } as Partial<Display>)}
                  >
                    {words}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
