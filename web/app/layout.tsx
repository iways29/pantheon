import type { Metadata, Viewport } from 'next';
import { Hanken_Grotesk, Marcellus } from 'next/font/google';
import type { ReactNode } from 'react';

import './globals.css';

// Self-hosted at build time by next/font: no request to Google at runtime.
const display = Marcellus({ weight: '400', subsets: ['latin'], variable: '--font-display' });
const body = Hanken_Grotesk({
  weight: ['400', '500', '600'],
  subsets: ['latin'],
  variable: '--font-body',
});

export const metadata: Metadata = {
  title: 'Pantheon',
  description: 'The brain of The Unreal Lab: what its agents know and do.',
};

export const viewport: Viewport = {
  themeColor: [
    { media: '(prefers-color-scheme: dark)', color: '#070B18' },
    { media: '(prefers-color-scheme: light)', color: '#E9ECF3' },
  ],
};

/**
 * The owner's display settings (theme, motion, transparency) live in this
 * browser only. They are applied before the first paint so a light-mode owner
 * never sees a dark flash.
 */
const applySettings = `
try {
  var s = JSON.parse(localStorage.getItem('pantheon.display') || '{}');
  var r = document.documentElement;
  r.dataset.theme = s.theme === 'light' ? 'light' : 'dark';
  if (s.motion === 'reduced') r.classList.add('rm');
  if (s.glass === 'solid') r.classList.add('solid');
} catch (e) { document.documentElement.dataset.theme = 'dark'; }
`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      data-theme="dark"
      className={`${display.variable} ${body.variable}`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: applySettings }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
