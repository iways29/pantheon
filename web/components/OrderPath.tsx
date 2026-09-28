'use client';

import { useCallback, useEffect, useState } from 'react';

import { Mark, type MarkKind } from '@/components/Mark';
import { api } from '@/lib/api';
import { useCompany } from '@/lib/company';
import { agentName, departmentName, money, time } from '@/lib/format';

interface Step {
  id: string;
  parent_id: string | null;
  depth: number;
  title: string;
  status: string;
  agent: string | null;
  department: string | null;
  summary: string | null;
  at: string;
  finished_at: string | null;
  cost_usd: number;
}

interface Path {
  id: string;
  steps: Step[];
  cost_usd: number;
}

interface Latest {
  id: string;
  title: string;
  text: string;
  status: string;
  at: string;
}

const MARK: Record<string, MarkKind> = {
  queued: 'todo',
  running: 'work',
  blocked: 'work',
  awaiting_approval: 'wait',
  done: 'done',
  failed: 'stop',
  cancelled: 'stop',
};

const WORDS: Record<string, string> = {
  queued: 'about to start',
  running: 'working',
  blocked: 'waiting on its team',
  awaiting_approval: 'asked you',
  done: 'done',
  failed: 'failed',
  cancelled: 'cancelled',
};

const MOVES = new Set(['task_queued', 'task_running', 'task_done', 'task_failed', 'task_blocked', 'order_routed', 'order_question']);

/** The last order and its path through the company, with what each step cost. */
export function OrderPath() {
  const { onEvent, preview } = useCompany();
  const [order, setOrder] = useState<Latest | null>(null);
  const [path, setPath] = useState<Path | null>(null);

  const load = useCallback(async () => {
    if (preview) return;
    try {
      const [latest] = await api.get<Latest[]>('orders?limit=1');
      setOrder(latest ?? null);
      setPath(latest ? await api.get<Path>(`orders/${latest.id}/path`) : null);
    } catch {
      setPath(null);
    }
  }, [preview]);

  useEffect(() => {
    void load();
    return onEvent((event) => {
      if (MOVES.has(event.type)) void load();
    });
  }, [load, onEvent]);

  if (!order || !path) return null;
  const moving = !['done', 'failed', 'cancelled'].includes(order.status);

  return (
    <section className="order-path" aria-labelledby="order-h">
      <div className="rule" />
      <h2 id="order-h" className="disp" style={{ fontSize: 18 }}>
        {moving ? 'Order in motion' : 'Last order'}
      </h2>
      <p style={{ fontSize: 14, lineHeight: 1.4 }} className="clamp">
        {order.text}
      </p>
      <ol className="steps" aria-label="Order path">
        <li className="step">
          <Mark kind="done" />
          <span>
            <span className="w">You</span> <span className="d">gave the order, {time(order.at)}</span>
          </span>
          <span />
        </li>
        {path.steps.map((s) => (
          <li key={s.id} className="step" style={{ paddingLeft: Math.min(s.depth, 3) * 14 }}>
            <Mark kind={MARK[s.status] ?? 'todo'} />
            <span>
              <span className="w">{agentName(s.agent)}</span>{' '}
              <span className="d">
                {s.depth === 0 && s.department ? `, ${departmentName(s.department)}: ` : ''}
                {WORDS[s.status] ?? s.status}
              </span>
            </span>
            <span className="faint num" style={{ fontSize: 12 }}>
              {s.cost_usd ? money(s.cost_usd) : ''}
            </span>
          </li>
        ))}
      </ol>
      <div className="row-between order-total">
        <span className="muted">{moving ? 'So far' : 'Whole chain'}</span>
        <span className="num" style={{ fontWeight: 600 }}>
          {money(path.cost_usd)}
        </span>
      </div>
    </section>
  );
}
