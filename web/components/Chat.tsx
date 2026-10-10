'use client';

import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

import { FollowButton } from '@/components/FollowButton';
import { Icon } from '@/components/Icon';
import { AGENT_MARK, AGENT_WORDS, Mark } from '@/components/Mark';
import { api, stream } from '@/lib/api';
import { useCompany } from '@/lib/company';
import { previewThread, previewTalkers } from '@/lib/fixtures';
import { agentName, departmentName, money, when } from '@/lib/format';
import type { ChatMessage, Conversation, OrderCard, OrderPiece, OrderQuestion, Talker } from '@/lib/types';

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
  /** "New chat": an empty view; the conversation is made by the first message.
   * The chat opens fresh (owner, 2026-10-05); past chats are in the history.
   * The preview opens on its sample thread, to compare with the designs. */
  const [fresh, setFresh] = useState(!preview);
  const [history, setHistory] = useState<Conversation[] | null>(null);
  const [latestId, setLatestId] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const sentAt = useRef(0);
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

  // "Talk" on an agent's panel opens a fresh chat with it here.
  useEffect(() => {
    const onTalk = (event: Event) => {
      const name = (event as CustomEvent<string>).detail;
      setWho(name);
      setConversation(null);
      setFresh(true);
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

  // Blank the thread only when the talker changes: a message landing in a
  // new conversation must not flash the chat empty.
  const loadedFor = useRef<string | null>(null);
  useEffect(() => {
    if (loadedFor.current !== who) setThread(null);
    loadedFor.current = who;
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
    // A reload from the live stream can bring this message in before the
    // reply's "done": from then on the saved copy is shown, not the bubble.
    sentAt.current = Date.now() - 5000;
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
            // The two messages are saved: show them now and drop the bubbles.
            // The stream stays open a few seconds more while the message is
            // sorted into memory; waiting for its end showed both at once.
            const { said: saved, reply } = data as { said: ChatMessage; reply: ChatMessage };
            setThread((list) => [...(list ?? []).filter((m) => m.id !== saved.id && m.id !== reply.id), saved, reply]);
            setWaiting(null);
            setStreamed('');
            // Now on the conversation this message landed in (new, or started
            // after a quiet gap).
            const landed = saved.conversation_id;
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
      <HintLayer />
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
                      setFresh(true);
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
        {waiting && !thread?.some((m) => m.role === 'owner' && m.text === waiting && Date.parse(m.at) >= sentAt.current) ? (
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
      {order.routed.length ? <FollowButton id={order.id} title={order.title} /> : null}
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
      {order.waiting?.length ? (
        <div className="order-q">
          {order.waiting.map((w) => (
            <div key={w.approval_id} className="row-between" style={{ alignItems: 'center' }}>
              <span style={{ fontSize: 13 }}>
                <span style={{ color: 'var(--ice)' }}>
                  {w.kind === 'draft_review' ? 'A draft waits for your yes' : 'Waits for your yes'}
                </span>
                <br />
                {w.title}
              </span>
              <button
                className="btn sm glass"
                onClick={() => window.dispatchEvent(new CustomEvent('pantheon:approval', { detail: w.approval_id }))}
              >
                Open
              </button>
            </div>
          ))}
        </div>
      ) : null}
      {order.pieces?.map((piece) => <Piece key={piece.id} piece={piece} />)}
      {order.media?.length ? (
        <div className="order-media">
          {order.media.map((m) => (
            <a key={m.url} href={m.url} target="_blank" rel="noopener noreferrer" title="Open full size">
              <img src={m.thumb} alt="An image the work made" loading="lazy" referrerPolicy="no-referrer" />
            </a>
          ))}
        </div>
      ) : null}
      {order.status === 'done' && order.result && !order.waiting?.length ? (
        <Summary text={order.result} />
      ) : null}
      {order.status === 'failed' && order.error ? (
        <p style={{ fontSize: 13, color: 'var(--verm)' }}>{order.error}</p>
      ) : null}
    </div>
  );
}

const PIECE_WORDS: Record<string, string> = {
  draft: 'Written, not checked yet',
  blocked: 'Stopped by a check',
  ready: 'Ready for your yes',
  approved: 'Approved',
  rejected: 'Rejected',
  published: 'Published',
};

/** Why a check stopped a line, in the owner's words. */
function why(reason: string): string {
  const r = reason.toLowerCase();
  if (r.includes('public facts') || r.includes('support')) return 'not backed by the company’s public facts';
  if (r.includes('banned')) return 'uses a banned word';
  if (r.includes('voice')) return 'off-voice';
  return r;
}

/**
 * What the work made, in full: the owner reads the song, not its id. Lines a
 * check stopped are marked in the text itself, with one sentence saying why,
 * instead of a red list under it (owner, 2026-09-27).
 */
function Piece({ piece }: { piece: OrderPiece }) {
  const [open, setOpen] = useState(false);
  const body = piece.body.replace(/\*\*(.+?)\*\*/g, '$1');
  const stopped = piece.stopped.filter((line) => line.sentence);
  const reasons = [...new Set(stopped.map((line) => why(line.reason)))];
  /** Why the fact check held this line back, or null when it did not. */
  const heldFor = (line: string) => {
    const hit = stopped.find((s) => line.includes(s.sentence.replace(/[.!?]+$/, '')));
    return hit ? `Held back by the fact check: ${why(hit.reason)}. Not published.` : null;
  };
  return (
    <div className="order-q piece">
      <span className="faint" style={{ fontSize: 12 }}>
        {PIECE_WORDS[piece.status] ?? piece.status}
        {piece.channel && piece.channel !== 'other' ? ` · ${piece.channel}` : ''}
      </span>
      {piece.title ? <span style={{ fontWeight: 500 }}>{piece.title}</span> : null}
      {stopped.length ? (
        // The why lives in a bubble, not in the text (owner, 2026-10-05).
        <span
          className="hint-chip"
          tabIndex={0}
          data-hint={`The fact check held back ${stopped.length === 1 ? 'one line' : `${stopped.length} lines`}: ${reasons.join('; ')}. Nothing was published. Hover a marked line to see why.`}
        >
          <span className="hint-dot verm" aria-hidden="true" />
          {stopped.length === 1 ? '1 line held back' : `${stopped.length} lines held back`}
        </span>
      ) : null}
      <div className={`piece-body${open ? ' open' : ''}`}>
        {body.split('\n').map((line, i) => {
          const held = line.trim() ? heldFor(line) : null;
          return held ? (
            <mark key={i} className="stopped" tabIndex={0} data-hint={held}>
              {line}
              {'\n'}
            </mark>
          ) : (
            <span key={i}>
              {line}
              {'\n'}
            </span>
          );
        })}
      </div>
      {body.length > 600 || body.split('\n').length > 10 ? (
        <button type="button" className="text-link" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? 'Show less' : 'Read it all'}
        </button>
      ) : null}
    </div>
  );
}

/** A report's **bold** as bold and its web addresses as links; the rest as
 * text (React escapes it). */
function rich(text: string): ReactNode[] {
  return text.split(/(\[(?:unverified|note):[^\]\n]+\]|\*\*[^*\n]+\*\*|https?:\/\/[^\s<>"')\]]+)/gi).map((part, i) => {
    // A caveat on a point ("[Unverified: the time]"): a small i, read on hover.
    const caveat = /^\[(unverified|note):\s*([^\]]+)\]$/i.exec(part);
    if (caveat)
      return (
        <span key={i} className="hint-i" tabIndex={0} data-hint={`${caveat[1]![0]!.toUpperCase()}${caveat[1]!.slice(1).toLowerCase()}: ${caveat[2]!.trim()}`}>
          i
        </span>
      );
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4)
      return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (/^https?:\/\//.test(part)) {
      const url = part.replace(/[.,;:]+$/, '');
      return (
        <span key={i}>
          <a href={url} target="_blank" rel="noopener noreferrer" className="inline-link">
            {url}
          </a>
          {part.slice(url.length)}
        </span>
      );
    }
    return part;
  });
}

/** The report, three lines until the owner opens it. */
function Summary({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  // The team's housekeeping (facts added, checks run, sub-tasks) is a line the
  // lead starts with "Team notes:"; it goes in a bubble, not the answer.
  const lines = text.split('\n');
  const notes = lines.filter((l) => TEAM_NOTES.test(l)).map((l) => l.replace(TEAM_NOTES, '').trim());
  const answer = lines
    .filter((l) => !TEAM_NOTES.test(l))
    .join('\n')
    .trim();
  const long = answer.length > 180;
  return (
    <div style={{ display: 'grid', gap: 4, justifyItems: 'start' }}>
      <p className={open ? undefined : 'clamp'} style={{ fontSize: 13, lineHeight: 1.45, whiteSpace: 'pre-wrap' }}>
        {rich(answer)}
      </p>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        {long ? (
          <button type="button" className="text-link" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? 'Show less' : 'Read all'}
          </button>
        ) : null}
        {notes.length ? (
          <span className="hint-chip" tabIndex={0} data-hint={notes.join(' ')}>
            <span className="hint-i" aria-hidden="true">i</span>
            Team notes
          </span>
        ) : null}
      </div>
    </div>
  );
}

const TEAM_NOTES = /^\s*[-*]?\s*\**team notes:?\**:?\s*/i;

/**
 * One bubble for every [data-hint] in the chat: shown on hover, or on focus
 * (a tap, or the keyboard), placed by the page so a card's edges never cut it.
 */
function HintLayer() {
  const [hint, setHint] = useState<{ text: string; x: number; y: number; below: boolean } | null>(null);
  useEffect(() => {
    const target = (event: Event) =>
      event.target instanceof Element ? (event.target.closest('[data-hint]') as HTMLElement | null) : null;
    const show = (event: Event) => {
      const el = target(event);
      if (!el?.dataset.hint) return;
      const box = el.getBoundingClientRect();
      const pointer = event instanceof PointerEvent ? event.clientX : box.left + Math.min(box.width, 240) / 2;
      const below = box.top < 140;
      setHint({
        text: el.dataset.hint,
        x: Math.min(Math.max(pointer, 150), window.innerWidth - 150),
        y: below ? box.bottom + 8 : box.top - 8,
        below,
      });
    };
    const hide = (event: Event) => {
      if (target(event)) setHint(null);
    };
    const clear = () => setHint(null);
    document.addEventListener('pointerover', show);
    document.addEventListener('pointerout', hide);
    document.addEventListener('focusin', show);
    document.addEventListener('focusout', hide);
    window.addEventListener('scroll', clear, true);
    return () => {
      document.removeEventListener('pointerover', show);
      document.removeEventListener('pointerout', hide);
      document.removeEventListener('focusin', show);
      document.removeEventListener('focusout', hide);
      window.removeEventListener('scroll', clear, true);
    };
  }, []);
  if (!hint) return null;
  // On the page itself: inside the chat's glass (a backdrop filter) a fixed
  // bubble would be placed and clipped by the panel.
  return createPortal(
    <div
      className="hint-bubble"
      role="tooltip"
      style={{ left: hint.x, top: hint.y, transform: hint.below ? 'translate(-50%, 0)' : 'translate(-50%, -100%)' }}
    >
      {hint.text}
    </div>,
    document.body,
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

