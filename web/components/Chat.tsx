'use client';

import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';

import { Icon } from '@/components/Icon';
import { AGENT_MARK, AGENT_WORDS, Mark } from '@/components/Mark';
import { api, stream } from '@/lib/api';
import { useCompany } from '@/lib/company';
import { previewThread, previewTalkers } from '@/lib/fixtures';
import { agentName, departmentName, money, when } from '@/lib/format';
import type { ChatMessage, OrderCard, OrderQuestion, Talker } from '@/lib/types';

/** Events after which a thread may have moved on. */
const THREAD_EVENTS = new Set([
  'chat_replied',
  'order_routed',
  'order_question',
  'task_done',
  'task_failed',
  'task_cancelled',
  'approval_decided',
  'killed',
]);

function title(talker: Talker): string {
  return talker.role === 'chief_of_staff'
    ? 'Chief of Staff'
    : `${departmentName(talker.department)} head`;
}

/**
 * The chat (ADR 037): talk with the Chief of Staff or any department head.
 * Answers come back in seconds; asking for work starts it at once, and the
 * work's card follows it here: its route, any question, its result and cost.
 */
export type PanelMode = 'open' | 'max' | 'min';

export function Chat({ mode = 'open', onMode }: { mode?: PanelMode; onMode?: (mode: PanelMode) => void } = {}) {
  const { agents, onEvent, preview } = useCompany();
  const [talkers, setTalkers] = useState<Talker[]>([]);
  const [who, setWho] = useState<string | null>(null);
  const [thread, setThread] = useState<ChatMessage[] | null>(null);
  const [text, setText] = useState('');
  const [waiting, setWaiting] = useState<string | null>(null);
  const [streamed, setStreamed] = useState('');
  const [menu, setMenu] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menu) return;
    const close = (event: MouseEvent) => {
      if (picker.current && !picker.current.contains(event.target as Node)) setMenu(false);
    };
    const onEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setMenu(false);
    };
    window.addEventListener('mousedown', close);
    window.addEventListener('keydown', onEscape);
    return () => {
      window.removeEventListener('mousedown', close);
      window.removeEventListener('keydown', onEscape);
    };
  }, [menu]);
  const [error, setError] = useState<string | null>(null);
  const log = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (preview) {
      const list = previewTalkers();
      setTalkers(list);
      setWho(list[0]?.name ?? null);
      return;
    }
    api
      .get<Talker[]>('chat')
      .then((list) => {
        setTalkers(list);
        setWho((current) => current ?? list[0]?.name ?? null);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [preview]);

  // "Talk" on an agent's panel opens its conversation here.
  useEffect(() => {
    const onTalk = (event: Event) => {
      const name = (event as CustomEvent<string>).detail;
      setWho(name);
      setMenu(false);
      box.current?.focus();
    };
    window.addEventListener('pantheon:talk', onTalk);
    return () => window.removeEventListener('pantheon:talk', onTalk);
  }, []);

  const load = useCallback(async () => {
    if (!who) return;
    if (preview) {
      setThread(previewThread(who, preview));
      return;
    }
    try {
      setThread(await api.get<ChatMessage[]>(`chat/${encodeURIComponent(who)}?limit=60`));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [who, preview]);

  useEffect(() => {
    setThread(null);
    void load();
    return onEvent((event) => {
      if (THREAD_EVENTS.has(event.type)) void load();
    });
  }, [load, onEvent]);

  const count = (thread?.length ?? 0) + (waiting ? 2 : 0);
  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight });
  }, [count, streamed]);

  // The box grows with what is written, up to six lines.
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
  }, [text]);

  async function send(event?: FormEvent) {
    event?.preventDefault();
    const said = text.trim();
    if (!said || waiting || !who) return;
    setWaiting(said);
    setStreamed('');
    setText('');
    setError(null);
    try {
      if (preview) throw new Error('The preview does not send messages.');
      let failed: string | null = null;
      await stream(
        `chat/${encodeURIComponent(who)}/stream`,
        { text: said, key: crypto.randomUUID() },
        (kind, data) => {
          if (kind === 'delta') setStreamed((so) => so + (data as { text: string }).text);
          if (kind === 'error') failed = String((data as { detail: unknown }).detail);
        },
      );
      if (failed) throw new Error(failed);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setText(said);
    } finally {
      setWaiting(null);
      setStreamed('');
    }
  }

  function onKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter starts a new line.
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send();
    }
  }

  async function answer(question: OrderQuestion, department: string | null) {
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

  const talker = talkers.find((t) => t.name === who);
  const agent = agents.find((a) => a.name === who);

  return (
    <aside className="glass panel chat" aria-label="Chat">
      <div className="chat-head" ref={picker}>
        {agent ? <Mark kind={AGENT_MARK[agent.state]} size={12} /> : null}
        <div style={{ display: 'grid', gap: 2, flexGrow: 1, minWidth: 0 }}>
          <button
            className="who-button"
            aria-haspopup="listbox"
            aria-expanded={menu}
            aria-label={`Talking with ${talker ? title(talker) : 'nobody yet'}. Change`}
            onClick={() => setMenu((v) => !v)}
            disabled={talkers.length < 2}
          >
            <span className="disp">{talker ? title(talker) : 'Chat'}</span>
            {talkers.length > 1 ? <Icon name="chevron" size={16} /> : null}
          </button>
          <span className="faint" style={{ fontSize: 12 }}>
            {waiting
              ? 'Thinking'
              : agent
                ? agent.state === 'idle'
                  ? 'Idle, listening'
                  : AGENT_WORDS[agent.state]
                : ''}
          </span>
        </div>
        {onMode ? (
          <div className="head-buttons">
            <button
              className="btn ibtn sm glass"
              aria-label={mode === 'max' ? 'Back to the brain' : 'Make the chat bigger'}
              onClick={() => onMode(mode === 'max' ? 'open' : 'max')}
            >
              <Icon name={mode === 'max' ? 'shrink' : 'expand'} size={14} />
            </button>
            <button className="btn ibtn sm glass" aria-label="Fold the chat away" onClick={() => onMode('min')}>
              <Icon name="minus" size={14} />
            </button>
          </div>
        ) : null}
        {menu ? (
          <ul className="glass who-menu" role="listbox" aria-label="Talk with">
            {talkers.map((t) => {
              const state = agents.find((a) => a.name === t.name)?.state;
              return (
                <li key={t.name}>
                  <button
                    role="option"
                    aria-selected={t.name === who}
                    className="who-option"
                    onClick={() => {
                      setWho(t.name);
                      setMenu(false);
                    }}
                  >
                    {state ? <Mark kind={AGENT_MARK[state]} /> : <span className="gl idle" />}
                    <span style={{ display: 'grid' }}>
                      <span>{title(t)}</span>
                      <span className="faint" style={{ fontSize: 12 }}>
                        {agentName(t.name)}
                        {t.last ? ` · ${when(t.last.at)}` : ''}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}
      </div>

      <div ref={log} role="log" aria-label="Messages" aria-live="polite" className="chat-log">
        {thread && thread.length === 0 && !waiting ? (
          <p className="faint" style={{ fontSize: 13, textAlign: 'center' }}>
            {talker?.role === 'head'
              ? `Ask ${agentName(talker.name)} anything about ${departmentName(talker.department)}, or give it work.`
              : 'Ask anything, or say what you want done.'}
          </p>
        ) : null}
        {thread?.map((m) => (
          <div key={m.id} className="msg-group">
            <div className={`msg ${m.role === 'owner' ? 'you' : 'cos'}`}>
              <span style={{ whiteSpace: 'pre-wrap' }}>{m.text}</span>
              <div className="num msg-time">{when(m.at)}</div>
            </div>
            {m.role === 'agent' && m.order ? <Card order={m.order} onAnswer={answer} /> : null}
          </div>
        ))}
        {waiting ? (
          <>
            <div className="msg you">
              <span style={{ whiteSpace: 'pre-wrap' }}>{waiting}</span>
            </div>
            {streamed ? (
              <div className="msg cos" aria-busy="true">
                <span style={{ whiteSpace: 'pre-wrap' }}>{streamed}</span>
              </div>
            ) : (
              <div className="msg cos typing" role="status" aria-label="Thinking">
                <span />
                <span />
                <span />
              </div>
            )}
          </>
        ) : null}
      </div>

      <form className="chat-form" onSubmit={send}>
        <label htmlFor="order-input" className="vh">
          Message
        </label>
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
          <textarea
            id="order-input"
            ref={box}
            className="inp chat-box"
            rows={1}
            placeholder={talker?.role === 'head' ? 'Ask, or give work' : 'Ask, or give an order'}
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKey}
            maxLength={4000}
          />
          <button
            className="btn pri ibtn"
            aria-label="Send"
            style={{ width: 44, height: 44, flex: 'none' }}
            disabled={Boolean(waiting) || !text.trim() || !who}
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
            Enter sends · Shift+Enter for a new line
          </span>
        )}
      </form>
    </aside>
  );
}

const STATUS_WORDS: Record<string, string> = {
  queued: 'Starting',
  running: 'Working',
  blocked: 'Waiting on its team',
  awaiting_approval: 'Waiting for you',
  done: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

/** The work a message started: where it went, what it asks, how it ended. */
function Card({
  order,
  onAnswer,
}: {
  order: OrderCard;
  onAnswer: (question: OrderQuestion, department: string | null) => void;
}) {
  const pending = order.questions.find((q) => q.status === 'pending');
  const mark =
    order.status === 'done'
      ? 'done'
      : order.status === 'failed' || order.status === 'cancelled'
        ? 'stop'
        : pending || order.status === 'awaiting_approval'
          ? 'wait'
          : 'work';
  const routed = order.routed[order.routed.length - 1];
  return (
    <div className="order-card g2">
      <div className="kicker">
        <Mark kind={mark} />
        {pending ? 'Waiting for you' : (STATUS_WORDS[order.status] ?? order.status)}
        {routed ? <span className="faint">· {departmentName(routed.department)}</span> : null}
        <span className="faint num" style={{ marginLeft: 'auto' }}>
          {money(order.cost_usd)}
        </span>
      </div>
      <span style={{ fontWeight: 500, lineHeight: 1.35 }}>{order.title}</span>
      {pending ? (
        <div className="order-q">
          <span style={{ color: 'var(--ice)', fontSize: 13 }}>
            {pending.text || 'Which department should take this?'}
          </span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {pending.options.map((o) => (
              <button
                key={o.department}
                className="btn sm glass"
                onClick={() => onAnswer(pending, o.department)}
              >
                {departmentName(o.department)}
                {o.department === pending.recommended ? (
                  <span className="faint" style={{ fontSize: 11 }}>
                    suggested
                  </span>
                ) : null}
              </button>
            ))}
            <button className="btn sm glass" onClick={() => onAnswer(pending, null)}>
              Cancel it
            </button>
          </div>
        </div>
      ) : null}
      {order.status === 'done' && order.result ? (
        <p className="clamp" style={{ fontSize: 13, lineHeight: 1.45 }}>
          {order.result}
        </p>
      ) : null}
      {order.status === 'failed' && order.error ? (
        <p style={{ fontSize: 13, color: 'var(--verm)' }}>{order.error}</p>
      ) : null}
    </div>
  );
}
