'use client';

import { Header } from '@/components/Header';
import { PulseStrip } from '@/components/PulseStrip';

const SECTIONS: [string, string[]][] = [
  ['Company', ['Departments', 'Agents', 'Prompts', 'Morning routine', 'Knowledge']],
  ['Spend and safety', ['Models and spend', 'Tools and MCP', 'Autonomy and limits', 'Judge', 'Standing rules']],
  ['Outbound', ['Newsletter and email']],
  ['Record', ['Change log']],
];

/**
 * The Control Center tab (Step 11). Its sections are built in Step 11; until
 * then the frame is here so the two tabs share one shell.
 */
export default function ControlPage() {
  return (
    <div className="screen">
      <Header tab="control" />
      <div className="body" style={{ gridTemplateColumns: '232px minmax(0, 1fr)' }}>
        <nav className="glass panel" aria-label="Control Center sections" style={{ padding: '14px 10px', gap: 2 }}>
          <div style={{ padding: '2px 12px 8px' }}>
            <div className="disp" style={{ fontSize: 19 }}>
              Control Center
            </div>
            <div className="faint" style={{ fontSize: 12, lineHeight: 1.4, marginTop: 3 }}>
              Every change is saved as a version and logged.
            </div>
          </div>
          {SECTIONS.map(([group, items]) => (
            <div key={group}>
              <div className="navg">{group}</div>
              {items.map((item) => (
                <span key={item} className="navi" aria-disabled="true">
                  {item}
                </span>
              ))}
            </div>
          ))}
        </nav>
        <section className="panel" style={{ justifyContent: 'center', alignItems: 'flex-start', padding: 32 }}>
          <div className="empty" style={{ maxWidth: 520 }}>
            <h1 className="disp" style={{ fontSize: 26 }}>
              Nothing to change here yet
            </h1>
            <p className="muted" style={{ lineHeight: 1.5 }}>
              The Control Center arrives in Step 11. Until then, departments, prompts, routines and
              tools are changed from the command line, and every change is still logged.
            </p>
          </div>
        </section>
      </div>
      <PulseStrip />
    </div>
  );
}
