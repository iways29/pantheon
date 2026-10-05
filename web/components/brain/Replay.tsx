'use client';

/**
 * Replay the day (Events.dc.html: "Drag the dial's hand to replay the day"):
 * today's real events, played again through the same handler the live stream
 * uses, so every thread and glow is one event, as it was. Nothing is made up
 * and nothing is fetched per event; quiet stretches are skipped.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { Icon } from '@/components/Icon';
import { time } from '@/lib/format';
import type { PantheonEvent } from '@/lib/types';

/** Replay speeds: minutes of the day per second of playback. */
export const SPEEDS = [1, 10, 60] as const;
/** A gap longer than this (in seconds of playback) is skipped. */
const SKIP_AFTER_S = 2;
/** How often the replay moves on, and the screen with it. */
const TICK_MS = 50;
/** At least this long between two replayed events, in playback: a busy
 * minute plays out one thread at a time instead of all at once (owner,
 * 2026-10-05: "slow and smooth"). The clock waits while a burst plays. */
const EVENT_GAP_MS = 600;

export interface Replay {
  /** The moment being replayed (ms since epoch), or null when live. */
  at: number | null;
  playing: boolean;
  speed: number;
  start: (at?: number) => void;
  stop: () => void;
  toggle: () => void;
  seek: (at: number) => void;
  setSpeed: (speed: number) => void;
  from: number;
  to: number;
}

export function useReplay(
  events: PantheonEvent[],
  dayStart: number,
  play: (event: PantheonEvent) => void,
): Replay {
  const [at, setAt] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<number>(SPEEDS[1]);
  // The loop reads these without restarting on every change.
  const state = useRef({ at: 0, next: 0, hold: 0, speed: SPEEDS[1] as number, events, play });
  state.current.events = events;
  state.current.play = play;
  state.current.speed = speed;

  const to = Date.now();

  const seek = useCallback((when: number) => {
    const s = state.current;
    s.at = Math.max(dayStart, Math.min(when, Date.now()));
    s.hold = 0;
    // Seeking never fires what lies between: it only moves the cursor.
    s.next = s.events.findIndex((e) => Date.parse(e.created_at) > s.at);
    if (s.next < 0) s.next = s.events.length;
    setAt(s.at);
  }, [dayStart]);

  const start = useCallback(
    (when?: number) => {
      seek(when ?? dayStart);
      setPlaying(when === undefined);
    },
    [seek, dayStart],
  );

  const stop = useCallback(() => {
    setPlaying(false);
    setAt(null);
  }, []);

  useEffect(() => {
    if (!playing || at === null) return;
    // A timer, not animation frames: it keeps time in a background tab too.
    let last = performance.now();
    let timer = setTimeout(function tick() {
      const now = performance.now();
      const s = state.current;
      const dt = Math.min(now - last, 1000);
      last = now;
      const due = (i: number) => i < s.events.length && Date.parse(s.events[i]!.created_at) <= s.at;
      if (now >= s.hold) {
        // The clock moves only when the last event has had its moment.
        if (!due(s.next)) s.at += dt * s.speed * 60;
        // Skip a quiet stretch: jump to just before the next event.
        const upcoming = s.events[s.next];
        if (upcoming) {
          const gap = Date.parse(upcoming.created_at) - s.at;
          if (gap > SKIP_AFTER_S * 1000 * s.speed * 60) s.at = Date.parse(upcoming.created_at) - 500 * s.speed * 60;
        }
        if (due(s.next)) {
          s.play(s.events[s.next]!);
          s.next += 1;
          s.hold = now + EVENT_GAP_MS;
        }
      }
      if (s.at >= Date.now() || (s.next >= s.events.length && now >= s.hold)) {
        s.at = Math.min(s.at, Date.now());
        setAt(s.at);
        setPlaying(false);
        return;
      }
      // The screen shows whole minutes: it is drawn again only when the
      // replayed minute changes, not on every tick.
      setAt((shown) => (shown !== null && Math.floor(shown / 60000) === Math.floor(s.at / 60000) ? shown : s.at));
      timer = setTimeout(tick, TICK_MS);
    }, TICK_MS);
    return () => clearTimeout(timer);
  }, [playing, at === null]); // eslint-disable-line react-hooks/exhaustive-deps

  return {
    at,
    playing,
    speed,
    start,
    stop,
    toggle: () => {
      if (at === null) start();
      // At the end (or within a minute of now, where live events keep
      // arriving), play starts the day again.
      else if (!playing && (state.current.next >= state.current.events.length || at >= Date.now() - 60 * 1000)) {
        seek(dayStart);
        setPlaying(true);
      } else setPlaying(!playing);
    },
    seek,
    setSpeed,
    from: dayStart,
    to,
  };
}

function speedWords(speed: number): string {
  return speed === 60 ? '1 h/s' : `${speed} min/s`;
}

/** The player: play or pause, where in the day, how fast, back to live. */
export function ReplayBar({ replay, count }: { replay: Replay; count: number }) {
  if (replay.at === null) return null;
  const span = Math.max(replay.to - replay.from, 1);
  const step = 5 * 60 * 1000;
  return (
    <div className="glass replay-bar" role="group" aria-label="Replay of today">
      <button
        type="button"
        className="btn sm glass ibtn"
        onClick={replay.toggle}
        aria-label={replay.playing ? 'Pause the replay' : 'Play the replay'}
      >
        <Icon name={replay.playing ? 'pause' : 'play'} size={14} />
      </button>
      <input
        type="range"
        className="replay-scrub"
        min={0}
        max={span}
        step={60 * 1000}
        value={replay.at - replay.from}
        onChange={(e) => replay.seek(replay.from + Number(e.target.value))}
        onKeyDown={(e) => {
          if (e.key === 'PageUp' || e.key === 'PageDown') {
            e.preventDefault();
            replay.seek(replay.at! + (e.key === 'PageUp' ? step * 6 : -step * 6));
          }
        }}
        aria-label="Time of day"
        aria-valuetext={time(new Date(replay.at).toISOString())}
      />
      <span className="num replay-time" aria-live="off">
        {time(new Date(replay.at).toISOString())}
      </span>
      <button
        type="button"
        className="chip replay-speed"
        onClick={() => replay.setSpeed(SPEEDS[(SPEEDS.indexOf(replay.speed as (typeof SPEEDS)[number]) + 1) % SPEEDS.length]!)}
        aria-label={`Speed: ${speedWords(replay.speed)}. Change`}
        title={`${count} events today`}
      >
        {speedWords(replay.speed)}
      </button>
      <button type="button" className="btn sm glass" onClick={replay.stop}>
        <span className="run-dot" aria-hidden="true" />
        Live
      </button>
    </div>
  );
}
