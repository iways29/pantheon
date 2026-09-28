'use client';

import { Icon } from '@/components/Icon';
import { useCompany } from '@/lib/company';
import { agentName, when } from '@/lib/format';
import type { Approval } from '@/lib/types';

const RECOMMENDATION: Record<string, { words: string; icon: 'check' | 'cross' | 'eye' }> = {
  approve: { words: 'Jev: approve', icon: 'check' },
  reject: { words: 'Jev: reject', icon: 'cross' },
  look_closer: { words: 'Jev: look closer', icon: 'eye' },
};

function title(approval: Approval): string {
  const p = approval.payload as Record<string, unknown>;
  if (approval.action_type === 'fact_write') return String(p.claim ?? 'A fact to check');
  if (approval.action_type === 'route_order') return String(p.order ?? approval.task_title ?? '');
  return String(
    p.title ?? p.subject ?? approval.task_title ?? approval.action_type.replace(/_/g, ' '),
  );
}

/**
 * Everything waiting for the owner: approvals (with Jev's advice and any
 * clash with an earlier decision), questions from the Chief of Staff, and
 * MCP tools switched off because their server changed them.
 */
export function NeedsYou({
  onOpen,
  limit,
  onMore,
  onCollapse,
}: {
  onOpen?: (approval: Approval) => void;
  /** Show at most this many, with a way to the rest. */
  limit?: number;
  onMore?: () => void;
  /** Fold the list away to a small bubble. */
  onCollapse?: () => void;
}) {
  const { approvals, changedTools } = useCompany();
  const cap = limit ?? Infinity;
  const questions = approvals.filter((a) => a.action_type === 'route_order').slice(0, cap);
  const others = approvals
    .filter((a) => a.action_type !== 'route_order')
    .slice(0, Math.max(0, cap - questions.length));
  const total = approvals.length + changedTools.length;

  return (
    <section aria-labelledby="needs-h" className="needs">
      <div className="row-between">
        <h2 id="needs-h" className="disp" style={{ fontSize: 20 }}>
          Needs you
        </h2>
        <span style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="num" style={{ fontSize: 13, color: 'var(--ice)' }}>
            {total ? `${total} waiting` : 'none'}
          </span>
          {onCollapse ? (
            <button className="btn ibtn sm glass" aria-label="Fold Needs you away" onClick={onCollapse}>
              <Icon name="minus" size={14} />
            </button>
          ) : null}
        </span>
      </div>
      {total === 0 ? (
        <div className="g2" style={{ borderRadius: 16, padding: 16, display: 'grid', gap: 6 }}>
          <span>Nothing needs you.</span>
          <span className="faint" style={{ fontSize: 13 }}>
            When an agent needs a decision, it waits here and in chat.
          </span>
        </div>
      ) : (
        <ul className="needs-list">
          {questions.map((q) => (
            <li key={q.id}>
              <button className="card g2" onClick={() => onOpen?.(q)}>
                <span className="kicker">
                  <Icon name="chat" size={14} style={{ color: 'var(--ice)' }} />
                  Question from Chief of Staff
                  <span className="faint num" style={{ marginLeft: 'auto' }}>
                    {when(q.created_at)}
                  </span>
                </span>
                <span style={{ lineHeight: 1.35 }}>{q.explanation || title(q)}</span>
                <span className="faint" style={{ fontSize: 12 }}>
                  Tap to answer
                </span>
              </button>
            </li>
          ))}
          {others.map((a) => {
            const rec = a.recommendation ? RECOMMENDATION[a.recommendation] : undefined;
            const clash = a.conflicts?.[0];
            return (
              <li key={a.id}>
                <button className="card g2" onClick={() => onOpen?.(a)}>
                  <span className="kicker">
                    <span className="gl wait" aria-hidden="true" />
                    {a.action_type === 'fact_write' ? 'Fact held for you' : 'Approval'}
                    {a.agent ? <span className="faint">· {agentName(a.agent)}</span> : null}
                    <span className="faint num" style={{ marginLeft: 'auto' }}>
                      {when(a.created_at)}
                    </span>
                  </span>
                  <span className="clamp" style={{ fontWeight: 500, lineHeight: 1.35 }}>
                    {title(a)}
                  </span>
                  {rec || clash ? (
                    <span style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      {rec ? (
                        <span className="rec">
                          <Icon name={rec.icon} size={14} />
                          {rec.words}
                        </span>
                      ) : null}
                      {clash ? (
                        <span className="clash">
                          <Icon name="warn" size={14} />
                          Clashes with {clash.decided_at ? when(clash.decided_at) : 'a decision'}
                        </span>
                      ) : null}
                    </span>
                  ) : null}
                </button>
              </li>
            );
          })}
          {changedTools.map((tool) => (
            <li key={tool.name}>
              <a className="card g2" href="/control" style={{ textDecoration: 'none' }}>
                <span className="kicker">
                  <Icon name="tool" size={14} style={{ color: 'var(--ice)' }} />
                  Tool changed
                </span>
                <span style={{ lineHeight: 1.35 }}>{tool.name.replace(/_/g, ' ')}</span>
                <span className="faint" style={{ fontSize: 12 }}>
                  Off until you re-approve it
                </span>
              </a>
            </li>
          ))}
        </ul>
      )}
      {limit !== undefined && total > limit && onMore ? (
        <button className="btn sm glass" onClick={onMore}>
          See all {total}
        </button>
      ) : null}
    </section>
  );
}
