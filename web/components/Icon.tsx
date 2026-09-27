/** The design's line icons (16px grid unless noted). Decorative: aria-hidden. */

const PATHS = {
  pause: <path d="M5.5 3.5v9M10.5 3.5v9" />,
  play: <path d="M5 3.5l7 4.5-7 4.5z" />,
  kill: (
    <>
      <circle cx="8" cy="8" r="6" />
      <rect x="6" y="6" width="4" height="4" />
    </>
  ),
  settings: (
    <>
      <path d="M2.5 5h7M12.5 5h1M2.5 11h1M6.5 11h7" />
      <circle cx="11" cy="5" r="1.5" />
      <circle cx="5" cy="11" r="1.5" />
    </>
  ),
  close: <path d="M4 4l8 8M12 4l-8 8" />,
  chevron: <path d="M4 6l4 4 4-4" />,
  eye: (
    <>
      <path d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" />
      <circle cx="8" cy="8" r="2" />
    </>
  ),
  check: <path d="M3 8.5l3 3 7-7" />,
  cross: <path d="M4.5 4.5l7 7M11.5 4.5l-7 7" />,
  warn: <path d="M8 2.5l6 10.5H2zM8 7v2.5M8 11.3v.1" />,
  chat: (
    <path d="M2.5 4a1.5 1.5 0 011.5-1.5h8A1.5 1.5 0 0113.5 4v6a1.5 1.5 0 01-1.5 1.5H7l-3 2.5v-2.5A1.5 1.5 0 012.5 10z" />
  ),
  tool: (
    <path d="M10 2.5a3.5 3.5 0 00-3.3 4.6L2.5 11.3l2.2 2.2 4.2-4.2A3.5 3.5 0 0013.5 6l-2 2-1.8-.3-.3-1.8 2-2A3.5 3.5 0 0010 2.5z" />
  ),
  path: (
    <>
      <circle cx="3.5" cy="12.5" r="1.5" />
      <circle cx="12.5" cy="3.5" r="1.5" />
      <path d="M5 12.5h4a2.5 2.5 0 000-5H7a2.5 2.5 0 010-5h4" />
    </>
  ),
  replay: (
    <>
      <path d="M3 3v3.5h3.5" />
      <path d="M3.4 6.5A5.5 5.5 0 1 1 3 9" />
    </>
  ),
  send: <path d="M8 13V3M4 7l4-4 4 4" />,
  expand: <path d="M9.5 2.5h4v4M13.5 2.5L9 7M6.5 13.5h-4v-4M2.5 13.5L7 9" />,
  fact: (
    <>
      <circle cx="8" cy="8" r="2.5" />
      <path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2" />
    </>
  ),
  lock: (
    <>
      <rect x="3.5" y="7" width="9" height="6.5" rx="1.5" />
      <path d="M5.5 7V5a2.5 2.5 0 015 0v2" />
    </>
  ),
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({
  name,
  size = 16,
  style,
}: {
  name: IconName;
  size?: number;
  style?: React.CSSProperties;
}) {
  return (
    <svg
      className="ic"
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      style={style}
    >
      {PATHS[name]}
    </svg>
  );
}

/** Pantheon's mark: a dial with one hand (24px grid). */
export function Logo({ size = 26 }: { size?: number }) {
  return (
    <svg
      className="ic"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
      style={{ color: 'var(--gold)' }}
    >
      <circle cx="12" cy="12" r="10" />
      <circle cx="12" cy="12" r="5.5" />
      <path d="M12 2v3M12 12l4.5-5.5" />
    </svg>
  );
}
