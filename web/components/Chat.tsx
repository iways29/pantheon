'use client';

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';

import { Icon } from '@/components/Icon';
import { AGENT_MARK, AGENT_WORDS, Mark } from '@/components/Mark';
import { api } from '@/lib/api';
import { useCompany } from '@/lib/company';
import { previewOrders } from '@/lib/fixtures';
import { departmentName, money, when } from '@/lib/format';

interface Question {
  approval_id: string;
  status: string;
  text: string | null;
  recommended: string | null;
  options: { department: string; probability: number }[];
  verdict: string | null;
  at: string;
  decided_at: string | null;
}

interface Order {
  id: string;
  title: string;
  text: string;
  status: string;
  result: string | null;
  error: string | null;
  at: string;
  finished_at: string | null;
  routed: { department: string | null; at: string }[];
  questions: Question[];
  cost_usd: number;
}

type Message =
  | { key: string; kind: 'you' | 'cos'; text: string; at: string }
  | { key: string; kind: 'q'; text: string; at: string; question: Question }
  | { key: string; kind: 'sys'; text: string; at: string };

/** Events after which the conversation may have moved on. */
const ORDER_EVENTS = new Set([
  'order_routed',
  'order_question',
  'task_done',
  'task_failed',
  'task_cancelled',
  'approval_decided',
  'killed',
]);

function messages(orders: Order[]): Message[] {
  const out: Message[] = [];
  for (const order of [...orders].reverse()) {
    const own: Message[] = [{ key: `${order.id}:you`, kind: 'you', text: order.text, at: order.at }];
    for (const [i, q] of order.questions.entries()) {
      own.push({
        key: `${order.id}:q${i}`,
        kind: 'q',
        text: q.text || 'A question about this order',
        at: q.at,
        question: q,
      });
      if (q.status !== 'pending')
        own.push({
          key: `${order.id}:a${i}`,
          kind: 'sys',
          text: q.status === 'approved' ? 'You answered.' : q.status === 'expired' ? 'The question was cancelled.' : 'You cancelled the order.',
          at: q.decided_at ?? q.at,
        });
    }
    for (const [i, r] of order.routed.entries())
      own.push({ key: `${order.id}:r${i}`, kind: 'cos', text: `Routed to ${departmentName(r.department)}.`, at: r.at });
    const end = order.finished_at ?? order.at;
    if (order.status === 'done' && order.result)
      own.push({ key: `${order.id}:done`, kind: 'cos', text: `${order.result}\n\nWhole chain: ${money(order.cost_usd)}.`, at: end });
    if (order.status === 'failed')
      own.push({ key: `${order.id}:failed`, kind: 'cos', text: `This could not be done: ${order.error ?? 'no reason given'}.`, at: end });
    if (order.status === 'cancelled')
      own.push({ key: `${order.id}:cancelled`, kind: 'sys', text: 'This order was cancelled.', at: end });
    // In the order things happened; the order itself always first.
    out.push(own[0]!, ...own.slice(1).sort((a, b) => a.at.localeCompare(b.at)));
  }
  return out;
}

/** The chat with the Chief of Staff: orders in plain words, answers back. */
export function Chat() {
  const { agents, onEvent, preview } = useCompany();
  const [orders, setOrders] = useState<Order[] | null>(null);
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const log = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    if (preview) {
      setOrders(previewOrders(preview));
      return;
    }
    try {
      setOrders(await api.get<Order[]>('orders?limit=30'));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [preview]);

  useEffect(() => {
    void load();
    return onEvent((event) => {
      if (ORDER_EVENTS.has(event.type)) void load();
    });
  }, [load, onEvent]);

  const list = orders ? messages(orders) : [];
  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight });
  }, [list.length]);

  async function send(event: FormEvent) {
    event.preventDefault();
    const order = text.trim();
    if (!order || sending) return;
    setSending(true);
    setError(null);
    try {
      if (preview) throw new Error('The preview does not send orders.');
      await api.post('orders', { text: order });
      setText('');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  }

  async function answer(question: Question, department: string | null) {
    setError(null);
    try {
      if (preview) throw new Error('The preview does not send answers.');
      if (department) {
        await api.post(`approvals/${question.approval_id}/approve`, {
          edited_arguments: department === question.recommended ? null : { department },
        });
      } else {
        await api.post(`approvals/${question.approval_id}/reject`, { mode: 'cancel' });
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const chief = agents.find((a) => a.role === 'chief_of_staff');

  return (
    <aside className="glass panel chat" aria-label="Chat with the Chief of Staff">
      <div className="chat-head">
        {chief ? <Mark kind={AGENT_MARK[chief.state]} size={12} /> : null}
        <div style={{ display: 'grid', gap: 2, flexGrow: 1 }}>
          <h2 className="disp" style={{ fontSize: 18 }}>
            Chief of Staff
          </h2>
          <span className="faint" style={{ fontSize: 12 }}>
            {chief ? (chief.state === 'idle' ? 'Idle, listening for orders' : AGENT_WORDS[chief.state]) : ''}
          </span>
        </div>
      </div>
      <div ref={log} role="log" aria-label="Messages" className="chat-log">
        {orders && list.length === 0 ? (
          <p className="faint" style={{ fontSize: 13, textAlign: 'center' }}>
            No orders yet. Say what you want done, in plain words.
          </p>
        ) : null}
        {list.map((m) =>
          m.kind === 'sys' ? (
            <div key={m.key} className="sys">
              {m.text}
            </div>
          ) : m.kind === 'q' ? (
            <div key={m.key} className="msg q g2">
              <div className="kicker" style={{ color: 'var(--ice)', marginBottom: 6 }}>
                <span className="gl wait" aria-hidden="true" />
                Waiting for you
                <span className="faint num" style={{ marginLeft: 'auto' }}>
                  {when(m.at)}
                </span>
              </div>
              {m.text}
              {m.question.status === 'pending' ? (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
                  {m.question.options.map((o) => (
                    <button
                      key={o.department}
                      className="btn sm glass"
                      onClick={() => void answer(m.question, o.department)}
                    >
                      {departmentName(o.department)}
                      {o.department === m.question.recommended ? (
                        <span className="faint" style={{ fontSize: 11 }}>
                          suggested
                        </span>
                      ) : null}
                    </button>
                  ))}
                  <button className="btn sm glass" onClick={() => void answer(m.question, null)}>
                    Cancel the order
                  </button>
                </div>
              ) : null}
            </div>
          ) : (
            <div key={m.key} className={`msg ${m.kind}${m.kind === 'cos' ? ' g2' : ''}`}>
              <span style={{ whiteSpace: 'pre-wrap' }}>{m.text}</span>
              <div className="num msg-time">{when(m.at)}</div>
            </div>
          ),
        )}
      </div>
      <form className="chat-form" onSubmit={send}>
        <label htmlFor="order-input" className="vh">
          Order for the Chief of Staff
        </label>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <input
            id="order-input"
            className="inp"
            placeholder="Give an order in plain words"
            style={{ borderRadius: 999, padding: '0 18px' }}
            value={text}
            onChange={(event) => setText(event.target.value)}
            maxLength={4000}
          />
          <button
            className="btn pri ibtn"
            aria-label="Send order"
            style={{ width: 44, height: 44, flex: 'none' }}
            disabled={sending || !text.trim()}
          >
            <Icon name="send" />
          </button>
        </div>
        {error ? (
          <span role="alert" style={{ color: 'var(--verm)', fontSize: 12, paddingLeft: 18 }}>
            {error}
          </span>
        ) : (
          <span className="faint" style={{ fontSize: 12, paddingLeft: 18 }}>
            Each order shows its path in the brain.
          </span>
        )}
      </form>
    </aside>
  );
}
