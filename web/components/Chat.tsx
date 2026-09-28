'use client';

import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';

import { Icon } from '@/components/Icon';
import { AGENT_MARK, AGENT_WORDS, Mark } from '@/components/Mark';
import { api, stream } from '@/lib/api';
import { useCompany } from '@/lib/company';
import { previewThread, previewTalkers } from '@/lib/fixtures';
import { agentName, departmentName, money, when } from '@/lib/format';
import type { ChatMessage, Conversation, OrderCard, OrderQuestion, Talker } from '@/lib/types';

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
  /** The conversation on screen: null for the latest. */
  const [conversation, setConversation] = useState<string | null>(null);
  /** "New chat": an empty view; the conversation is made by the first message. */
  const [fresh, setFresh] = useState(false);
  const [history, setHistory] = useState<Conversation[] | null>(null);
  const [latestId, setLatestId] = useState<string | null>(null);
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
      setConversation(null);
      setFresh(false);
      setMenu(false);
      box.current?.focus();
    };
    window.addEventListener('pantheon:talk', onTalk);
    return () => window.removeEventListener('pantheon:talk', onTalk);
  }, []);

  const load = useCallback(async () => {
    if (!who) return;
    if (fresh) {
      setThread([]);
      return;
    }
    if (preview) {
      setThread(previewThread(who, preview));
      return;
    }
    try {
      const which = conversation ? `&conversation=${conversation}` : '';
      setThread(await api.get<ChatMessage[]>(`chat/${encodeURIComponent(who)}?limit=60${which}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [who, preview, fresh, conversation]);

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
        {
          text: said,
          key: crypto.randomUUID(),
          new: fresh,
          conversation_id: fresh ? null : conversation,
        },
        (kind, data) => {
          if (kind === 'delta') setStreamed((so) => so + (data as { text: string }).text);
          if (kind === 'done') {
            // Now on the conversation this message landed in (new, or started
            // after a quiet gap).
            const landed = (data as { said: ChatMessage }).said.conversation_id;
            setFresh(false);
            setConversation(landed ?? null);
            setLatestId(landed ?? null);
          }
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

  /** "New chat": a fresh view. Already fresh, or nothing said yet: nothing to do. */
  function newChat() {
    setHistory(null);
    if (fresh || !thread || thread.length === 0) return;
    setFresh(true);
    setConversation(null);
    setThread([]);
    box.current?.focus();
  }

  async function toggleHistory() {
    if (history !== null) {
      setHistory(null);
      return;
    }
    setMenu(false);
    if (preview || !who) {
      setHistory([]);
      return;
    }
    try {
      const list = await api.get<Conversation[]>(`chat/${encodeURIComponent(who)}/conversations`);
      setHistory(list);
      setLatestId(list[0]?.id ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const shown = fresh ? null : (conversation ?? thread?.[0]?.conversation_id ?? null);
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
        <div className="head-buttons">
          <button className="btn ibtn sm glass" aria-label="New chat" title="New chat" onClick={newChat}>
            <Icon name="plus" size={14} />
          </button>
          <button
            className="btn ibtn sm glass"
            aria-label="Past chats"
            title="Past chats"
            aria-expanded={history !== null}
            onClick={() => void toggleHistory()}
          >
            <Icon name="history" size={14} />
          </button>
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
                      setConversation(null);
                      setFresh(false);
                      setHistory(null);
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
        {history !== null ? (
          <ul className="glass who-menu history-menu" aria-label="Past chats">
            {history.length === 0 ? (
              <li className="faint" style={{ padding: '10px 12px', fontSize: 13 }}>
                No past chats yet.
              </li>
            ) : null}
            {history.map((c) => (
              <li key={c.id}>
                <button
                  className="who-option"
                  aria-current={c.id === shown ? 'true' : undefined}
                  onClick={() => {
                    setConversation(c.id);
                    setFresh(false);
                    setHistory(null);
                  }}
                >
                  <span style={{ display: 'grid', minWidth: 0 }}>
                    <span className="clamp1">{c.title}</span>
                    <span className="faint" style={{ fontSize: 12 }}>
                      {when(c.last_at)} · {c.messages} messages
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      {!fresh && conversation && latestId && conversation !== latestId ? (
        <div className="past-banner">
          <span className="faint">A past chat. Writing here carries it on.</span>
          <button className="chip" onClick={() => setConversation(null)}>
            Back to the latest
          </button>
        </div>
      ) : null}

      <div ref={log} role="log" aria-label="Messages" aria-live="polite" className="chat-log">
        {thread && thread.length === 0 && !waiting && talker ? <Welcome talker={talker} /> : null}
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

/**
 * What an empty chat opens with: a greeting and a line on today, from what the
 * screen already knows. Not stored, no model call.
 */
function Welcome({ talker }: { talker: Talker }) {
  const { pulse, agents } = useCompany();
  const hour = Number(
    new Intl.DateTimeFormat('en-GB', { hour: '2-digit', hourCycle: 'h23', timeZone: 'America/New_York' }).format(
      new Date(),
    ),
  );
  const greeting = hour < 5 ? 'Still up' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  let today: string;
  let offer: string;
  if (talker.role === 'chief_of_staff') {
    const waiting = pulse?.needs_you_total ?? 0;
    today = `${waiting ? `${waiting} ${waiting === 1 ? 'thing waits' : 'things wait'} for you` : 'Nothing waits for you'}, and ${money(pulse?.spend_usd ?? 0)} of today’s ${money(pulse?.budget_usd ?? 0)} is spent.`;
    offer = 'Ask me anything about the company, or tell me what you want done and I’ll hand it to the right department.';
  } else {
    const team = agents.filter((a) => a.department === talker.department);
    const busy = team.filter((a) => a.state === 'working').length;
    today = `${departmentName(talker.department)} has ${team.length} ${team.length === 1 ? 'agent' : 'agents'}${busy ? `, ${busy} at work now` : ', all quiet now'}.`;
    offer = `Ask me about ${departmentName(talker.department)}’s work, or give me something to do.`;
  }
  return (
    <div className="msg cos welcome" role="note">
      <span className="disp welcome-hi">{greeting}.</span>
      <span>
        {today} {offer}
      </span>
    </div>
  );
}

