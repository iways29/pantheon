'use client';

import { useEffect, useState } from 'react';

import { Icon } from '@/components/Icon';
import { AGENT_MARK, AGENT_WORDS, FACT_MARK, Mark } from '@/components/Mark';
import { api } from '@/lib/api';
import { useCompany } from '@/lib/company';
import { agentName, departmentName, money, when } from '@/lib/format';
import type { AgentState } from '@/lib/types';

interface Ref {
  id: string;
  claim: string;
  at: string;
}

interface FactDetail {
  id: string;
  claim: string;
  status: 'active' | 'disputed' | 'superseded';
  kind: string;
  public: boolean;
  source: string | null;
  source_ref: string | null;
  quote: string | null;
  document: { id: string; title: string } | null;
  at: string;
  found_by: { agent: string; department: string | null } | null;
  check: { gate: string; question: string; output: Record<string, unknown>; at: string } | null;
  replaces: Ref[];
  replaced_by: Ref | null;
  contradicts: Ref[];
  things: { id: string; kind: string; name: string }[];
  neighbours: (Ref & { similarity: number })[];
  place: { neighbourhood: string | null } | null;
}

interface AgentDetail {
  name: string;
  role: 'chief_of_staff' | 'head' | 'worker';
  department: string | null;
  state: AgentState;
  tier: string;
  level: string;
  current_task: { title: string; status: string } | null;
  last_results: { task_id: string; title: string; status: string; summary: string; finished_at: string | null; cost_usd: number }[];
  tools: string[];
  budget_usd: number | null;
  spent_today_usd: number;
  spent_7d_usd: number;
}

const STATUS_WORDS = { active: 'Fact, active', disputed: 'Fact, disputed', superseded: 'Fact, replaced' };
const KIND_WORDS: Record<string, string> = {
  preference: 'Your preference',
  rule: 'Your rule',
  interest: 'Your interest',
  style: 'How you work',
};

function useDetail<T>(path: string | null): { data: T | null; error: string | null } {
  const { preview } = useCompany();
  const [state, setState] = useState<{ data: T | null; error: string | null }>({ data: null, error: null });
  useEffect(() => {
    if (!path) return;
    setState({ data: null, error: null });
    if (preview) {
      setState({ data: null, error: 'Details load from the live API.' });
      return;
    }
    let live = true;
    api
      .get<T>(path)
      .then((data) => live && setState({ data, error: null }))
      .catch((err: unknown) => live && setState({ data: null, error: err instanceof Error ? err.message : String(err) }));
    return () => {
      live = false;
    };
  }, [path, preview]);
  return state;
}

function Sheet({ label, onClose, children }: { label: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <section className="glass sheet" aria-label={label}>
      <button className="btn ibtn sm glass sheet-close" aria-label="Close" onClick={onClose}>
        <Icon name="close" size={14} />
      </button>
      {children}
    </section>
  );
}

function isUrl(text: string | null): text is string {
  return Boolean(text && /^https?:\/\//.test(text));
}

/** Jev's check in words: the outcome it reached, when the output says. */
function verdict(output: Record<string, unknown>): string {
  const outcome = output.outcome ?? output.decision ?? output.verdict;
  if (typeof outcome === 'string') return outcome.replace(/_/g, ' ');
  return 'checked';
}

export function FactPanel({ id, onClose, onSelect }: { id: string; onClose: () => void; onSelect: (id: string) => void }) {
  const { snapshot } = useCompany();
  const held = id.startsWith('held:') ? snapshot?.held.find((h) => `held:${h.approval_id}` === id) : undefined;
  const { data: fact, error } = useDetail<FactDetail>(held ? null : `facts/${id}`);

  if (held) {
    return (
      <Sheet label="Held claim" onClose={onClose}>
        <div className="kicker">
          <Mark kind="f-held" />
          Held for you
        </div>
        <p className="disp sheet-title">{held.claim}</p>
        <p className="muted" style={{ fontSize: 13 }}>
          Jev was unsure about this claim, so it waits for your yes before it enters the brain. Decide it under Needs you.
        </p>
      </Sheet>
    );
  }

  return (
    <Sheet label="Fact detail" onClose={onClose}>
      {!fact ? (
        <p className="faint" style={{ fontSize: 13 }}>
          {error ?? 'Loading'}
        </p>
      ) : (
        <>
          <div className="kicker">
            <Mark kind={FACT_MARK[fact.status]} />
            {KIND_WORDS[fact.kind] ?? STATUS_WORDS[fact.status]}
            {fact.public ? <span className="faint">· public</span> : null}
          </div>
          <p className="disp sheet-title">{fact.claim}</p>
          <dl className="dl">
            <dt>Source</dt>
            <dd>
              {isUrl(fact.source_ref) ? (
                <a href={fact.source_ref} target="_blank" rel="noreferrer">
                  {fact.source_ref.replace(/^https?:\/\//, '').slice(0, 48)}
                </a>
              ) : (
                (fact.document?.title ?? fact.source ?? 'Not recorded')
              )}
            </dd>
            {fact.quote ? (
              <>
                <dt>From</dt>
                <dd className="muted">“{fact.quote.slice(0, 220)}”</dd>
              </>
            ) : null}
            <dt>Found by</dt>
            <dd>
              {fact.found_by
                ? `${agentName(fact.found_by.agent)}${fact.found_by.department ? `, ${departmentName(fact.found_by.department)}` : ''}`
                : fact.source === 'owner'
                  ? 'You'
                  : 'Added by you'}
            </dd>
            <dt>Admitted</dt>
            <dd className="num">{when(fact.at)}</dd>
            {fact.check ? (
              <>
                <dt>Jev’s check</dt>
                <dd>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontWeight: 500 }}>
                    <Icon name="check" size={14} style={{ color: 'var(--gold)' }} />
                    {verdict(fact.check.output)}
                  </span>{' '}
                  <span className="faint">({fact.check.question})</span>
                </dd>
              </>
            ) : null}
          </dl>
          {fact.replaces.length || fact.replaced_by || fact.contradicts.length || fact.neighbours.length || fact.things.length ? (
            <div className="rels">
              {fact.replaced_by ? (
                <Rel mark="f-act" label="Replaced by" item={fact.replaced_by} onSelect={onSelect} />
              ) : null}
              {fact.replaces.map((r) => (
                <Rel key={r.id} mark="f-sup" label="Replaced" item={r} onSelect={onSelect} />
              ))}
              {fact.contradicts.map((r) => (
                <Rel key={r.id} mark="f-dis" label="Contradicts" item={r} onSelect={onSelect} />
              ))}
              {fact.things.length ? (
                <div className="rel">
                  <span className="gl todo" aria-hidden="true" />
                  <span>
                    <span className="faint">About</span>
                    <br />
                    {fact.things.map((t) => t.name).join(', ')}
                  </span>
                </div>
              ) : null}
              {fact.neighbours[0] ? (
                <Rel
                  mark="f-act"
                  label={`Nearest${fact.place?.neighbourhood ? `, in ${fact.place.neighbourhood}` : ''}`}
                  item={fact.neighbours[0]}
                  onSelect={onSelect}
                />
              ) : null}
            </div>
          ) : null}
        </>
      )}
    </Sheet>
  );
}

function Rel({
  mark,
  label,
  item,
  onSelect,
}: {
  mark: 'f-act' | 'f-sup' | 'f-dis';
  label: string;
  item: Ref;
  onSelect: (id: string) => void;
}) {
  return (
    <button className="rel rel-btn" onClick={() => onSelect(item.id)}>
      <Mark kind={mark} />
      <span>
        <span className="faint">{label}</span>
        <br />
        {item.claim}
      </span>
    </button>
  );
}

const LEVELS: Record<string, string> = {
  L0: 'Asks you for almost everything',
  L1: 'Acts on its own data; asks before the outside world',
  L2: 'Reads the outside world with Jev’s check; asks before acting on it',
  L3: 'Reads the outside world on its own; spending always asks you',
};

export function AgentPanel({ name, onClose, onTalk }: { name: string; onClose: () => void; onTalk: (name: string) => void }) {
  const { agents } = useCompany();
  const { data, error } = useDetail<AgentDetail>(`agents/${encodeURIComponent(name)}/detail`);
  const fallback = agents.find((a) => a.name === name);
  const agent = data ?? fallback;
  const level = Number((agent?.level ?? 'L0').slice(1)) || 0;

  return (
    <Sheet label="Agent detail" onClose={onClose}>
      {!agent ? (
        <p className="faint">{error ?? 'Loading'}</p>
      ) : (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            <Mark kind={AGENT_MARK[agent.state]} size={14} />
            <div style={{ flexGrow: 1 }}>
              <h2 className="disp" style={{ fontSize: 22 }}>
                {agentName(agent.name)}
              </h2>
              <span className="faint" style={{ fontSize: 13 }}>
                {departmentName(agent.department)},{' '}
                {agent.role === 'chief_of_staff' ? 'Chief of Staff' : agent.role}
              </span>
            </div>
          </div>
          <div className="g2" style={{ borderRadius: 16, padding: '12px 14px', display: 'grid', gap: 4 }}>
            <span className="faint" style={{ fontSize: 12 }}>
              {AGENT_WORDS[agent.state]}
            </span>
            <span style={{ lineHeight: 1.4 }}>
              {agent.current_task ? agent.current_task.title : 'Nothing in hand.'}
            </span>
          </div>
          {data?.last_results.length ? (
            <div style={{ display: 'grid', gap: 8 }}>
              <span className="faint" style={{ fontSize: 12 }}>
                Last results
              </span>
              {data.last_results.slice(0, 3).map((r) => (
                <div key={r.task_id} className="rel">
                  <Mark kind={r.status === 'done' ? 'done' : 'stop'} />
                  <span>
                    {r.summary ? r.summary.slice(0, 160) : r.title}{' '}
                    <span className="faint num">
                      {when(r.finished_at)} · {money(r.cost_usd)}
                    </span>
                  </span>
                </div>
              ))}
            </div>
          ) : null}
          <dl className="dl">
            <dt>Tools</dt>
            <dd>{agent.tools.length ? agent.tools.map((t) => t.replace(/_/g, ' ')).join(', ') : 'None'}</dd>
            <dt>Autonomy</dt>
            <dd style={{ display: 'grid', gap: 6 }}>
              <span className="meter" role="img" aria-label={`Level ${level} of 3`}>
                {[0, 1, 2, 3].map((i) => (
                  <i key={i} className={i <= level ? 'on' : undefined} />
                ))}
              </span>
              <span className="muted" style={{ fontSize: 12 }}>
                {LEVELS[agent.level] ?? agent.level}
              </span>
            </dd>
            <dt>Spend today</dt>
            <dd className="num" style={{ fontWeight: 600 }}>
              {money(agent.spent_today_usd)}
              {data ? <span className="faint" style={{ fontWeight: 400 }}> · 7 days {money(data.spent_7d_usd)}</span> : null}
            </dd>
          </dl>
          {agent.role !== 'worker' ? (
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn sm glass" onClick={() => onTalk(agent.name)}>
                <Icon name="chat" size={14} />
                Talk
              </button>
            </div>
          ) : null}
        </>
      )}
    </Sheet>
  );
}
