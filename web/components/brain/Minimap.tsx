'use client';

import { useEffect, useRef } from 'react';

import type { MapFact } from '@/lib/types';

/**
 * Where the owner is in the brain (Field.dc.html's minimap): every fact from
 * above, a wedge for the way the view faces, and a ring that shrinks as the
 * owner zooms in. Tapping it brings the whole brain back into view.
 */
export function Minimap({
  facts,
  azimuth,
  zoom,
  onFit,
}: {
  facts: MapFact[];
  azimuth: number;
  zoom: number;
  onFit: () => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const c = canvas.current;
    const ctx = c?.getContext('2d');
    if (!c || !ctx) return;
    const size = 88;
    const ratio = window.devicePixelRatio || 1;
    c.width = size * ratio;
    c.height = size * ratio;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, size, size);
    const style = getComputedStyle(document.documentElement);
    const gold = style.getPropertyValue('--gold').trim() || '#E8BC62';
    const ice = style.getPropertyValue('--ice').trim() || '#94BBFF';
    const r = size / 2 - 6;
    ctx.fillStyle = gold;
    for (const f of facts) {
      if (f.x === null) continue;
      ctx.globalAlpha = f.status === 'superseded' ? 0.3 : 0.85;
      ctx.fillRect(size / 2 + f.x * r - 0.8, size / 2 + (f.z ?? 0) * r - 0.8, 1.6, 1.6);
    }
    ctx.globalAlpha = 1;
    // The way the view faces, and how close it is.
    ctx.strokeStyle = ice;
    ctx.lineWidth = 1.2;
    const reach = Math.max(0.12, Math.min(1, 1 / zoom)) * r;
    ctx.beginPath();
    ctx.arc(size / 2, size / 2, reach, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(size / 2, size / 2);
    ctx.lineTo(size / 2 + Math.sin(azimuth) * r, size / 2 + Math.cos(azimuth) * r);
    ctx.stroke();
  }, [facts, azimuth, zoom]);

  return (
    <button className="glass minimap" onClick={onFit} aria-label="Where you are in the brain. Show it all">
      <canvas ref={canvas} style={{ width: 88, height: 88 }} aria-hidden="true" />
    </button>
  );
}
