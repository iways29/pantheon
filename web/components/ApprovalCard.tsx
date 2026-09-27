'use client';

import { useEffect, useState } from 'react';

import { Icon } from '@/components/Icon';
import { Mark } from '@/components/Mark';
import { api } from '@/lib/api';
import { useCompany } from '@/lib/company';
import { agentName, departmentName, when } from '@/lib/format';
import type { Approval } from '@/lib/types';

const RECOMMEND: Record<string, { words: string; icon: 'check' | 'cross' | 'eye' }> = {
  approve: { words: 'Approve', icon: 'check' },
  reject: { words: 'Reject', icon: 'cross' },
  look_closer: { words: 'Look closer', icon: 'eye' },
};

const KIND: Record<string, string> = {
  fact_write: 'A fact held for you',
  route_order: 'A question from the Chief of Staff',
  draft_review: 'A draft to approve',
  send_email: 'An email to send',
  tool_call: 'An action to allow',
};

function headline(a: Approval): string {
  const p = a.payload as Record<string, unknown>;
  const pick = p.title ?? p.subject ?? p.claim ?? p.order ?? a.task_title;
  return pick ? String(pick) : (KIND[a.action_type] ?? a.action_type.replace(/_/g, ' '));
}

/** What the agent wants to do, in its own words, when the payload has them. */
function body(a: Approval): string | null {
  const p = a.payload as Record<string, unknown>;
  for (const key of ['text', 'body', 'draft', 'content', 'html_preview', 'preview']) {
    if (typeof p[key] === 'string' && p[key]) return String(p[key]);
  }
  if (a.action_type === 'tool_call' && p.arguments) {
    return `${String(p.tool ?? 'Tool')}: ${JSON.stringify(p.arguments, null, 1).slice(0, 600)}`;
  }
  return null;
}

/**
 * The decision card (ADR 021, design Approval.dc.html): what is asked, Jev's
 * recommendation, the facts checked, any clash with an earlier decision, and
 * the owner's choice: approve, reject, or redirect with a note. A decision
 * with a note is remembered in the brain.
 */
export function ApprovalCard({ approval, onClose }: { approval: Approval; onClose: () => void }) {
  const { refresh, preview } = useCompany();
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  async function decide(path: 'approve' | 'reject', payload: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    try {
      if (preview) throw new Error('The preview does not decide.');
      await api.post(`approvals/${approval.id}/${path}`, payload);
      await refresh();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  const rec = approval.recommendation ? RECOMMEND[approval.recommendation] : undefined;
  const checked = (approval.facts_checked ?? []).map((f) =>
    typeof f === 'string' ? f : String((f as { claim?: unknown }).claim ?? ''),
  );
  const clash = approval.conflicts?.[0];
  const text = body(approval);
  const question = approval.action_type === 'route_order';
  const options = ((approval.payload as { options?: { department: string }[] }).options ?? []).map(
    (o) => o.department,
  );
  const recommended = (approval.payload as { recommended?: string }).recommended;
  const trimmed = note.trim() || null;

  return (
    <section className="glass approval" role="dialog" aria-modal="false" aria-labelledby="approval-h">
      <div className="kicker">
        <Mark kind="wait" />
        {KIND[approval.action_type] ?? 'Approval'}
        {approval.agent ? <span className="faint">· {agentName(approval.agent)}</span> : null}
        <span className="faint num">{when(approval.created_at)}</span>
        <span style={{ flexGrow: 1 }} />
        <button className="btn ibtn sm glass" aria-label="Close" onClick={onClose}>
          <Icon name="close" size={14} />
        </button>
      </div>
      <h2 id="approval-h" className="disp" style={{ fontSize: 22, lineHeight: 1.25 }}>
        {question ? (approval.explanation ?? headline(approval)) : headline(approval)}
      </h2>
      {question ? (
        <blockquote className="g2 quote">{String((approval.payload as { order?: unknown }).order ?? '')}</blockquote>
      ) : text ? (
        <blockquote className="g2 quote">{text.slice(0, 1200)}</blockquote>
      ) : null}

      {rec && !question ? (
        <div style={{ display: 'grid', gap: 6 }}>
          <span className="faint" style={{ fontSize: 12 }}>
            Jev recommends
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 16, fontWeight: 600 }}>
            <Icon name={rec.icon} size={18} />
            {rec.words}
          </span>
          {approval.explanation ? (
            <span className="muted" style={{ fontSize: 13, lineHeight: 1.45 }}>
              {approval.explanation}
            </span>
          ) : null}
        </div>
      ) : null}

      {checked.length ? (
        <div style={{ display: 'grid', gap: 8 }}>
          <span className="faint" style={{ fontSize: 12 }}>
            Facts checked
          </span>
          {checked.slice(0, 5).map((claim) => (
            <div key={claim} className="rel">
              <Mark kind="f-act" />
              <span>{claim}</span>
            </div>
          ))}
        </div>
      ) : null}

      {clash ? (
        <div className="clash-box">
          <Icon name="warn" size={16} style={{ color: 'var(--verm)', marginTop: 2 }} />
          <div style={{ fontSize: 13, lineHeight: 1.45 }}>
            <span style={{ fontWeight: 600 }}>
              Clashes with your decision{clash.decided_at ? ` of ${when(clash.decided_at)}` : ''}
            </span>
            {clash.claim ? (
              <>
                <br />“{clash.claim}”
              </>
            ) : null}
          </div>
        </div>
      ) : null}

      <div style={{ display: 'grid', gap: 6 }}>
        <label htmlFor="approval-note" className="faint" style={{ fontSize: 12 }}>
          {question ? 'Or say what you meant' : 'Note'}
        </label>
        <textarea
          id="approval-note"
          className="inp"
          rows={2}
          placeholder="Optional. Say what to change, or why."
          value={note}
          onChange={(e) => setNote(e.target.value)}
          style={{ height: 60, padding: '10px 14px', resize: 'none', fontSize: 14, lineHeight: 1.4 }}
        />
      </div>

      {error ? (
        <p role="alert" style={{ color: 'var(--verm)', fontSize: 13 }}>
          {error}
        </p>
      ) : null}

      {question ? (
        <div className="actions" style={{ justifyContent: 'flex-start' }}>
          {options.map((d) => (
            <button
              key={d}
              className={`btn sm ${d === recommended ? 'pri' : 'glass'}`}
              disabled={busy}
              onClick={() =>
                void decide('approve', { edited_arguments: d === recommended ? null : { department: d }, note: trimmed })
              }
            >
              {departmentName(d)}
            </button>
          ))}
          <button
            className="btn sm glass"
            disabled={busy || !trimmed}
            title={trimmed ? undefined : 'Write what you meant first'}
            onClick={() => void decide('reject', { mode: 'redirect', note: trimmed })}
          >
            Send my note
          </button>
          <button className="btn sm glass" disabled={busy} onClick={() => void decide('reject', { mode: 'cancel' })}>
            Cancel the order
          </button>
        </div>
      ) : (
        <div className="decide">
          <button className="btn sm pri" disabled={busy} onClick={() => void decide('approve', { note: trimmed })}>
            <Icon name="check" size={14} />
            Approve
          </button>
          <button className="btn sm glass" disabled={busy} onClick={() => void decide('reject', { mode: 'cancel', note: trimmed })}>
            Reject
          </button>
          <button
            className="btn sm glass"
            disabled={busy || !trimmed}
            title={trimmed ? undefined : 'Write a note saying what to do instead'}
            onClick={() => void decide('reject', { mode: 'redirect', note: trimmed })}
          >
            Redirect
          </button>
        </div>
      )}
    </section>
  );
}
