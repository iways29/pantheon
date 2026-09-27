import type { AgentState, FactStatus } from '@/lib/types';

/**
 * A status mark. Each status has its own shape; colour only confirms it:
 * filled circle working or active, dashed ring disputed, hollow circle idle or
 * superseded, diamond waiting for the owner, two bars paused, square stopped.
 */
export type MarkKind =
  | 'idle'
  | 'work'
  | 'wait'
  | 'pause'
  | 'stop'
  | 'done'
  | 'todo'
  | 'f-act'
  | 'f-dis'
  | 'f-sup'
  | 'f-held';

export const AGENT_MARK: Record<AgentState, MarkKind> = {
  idle: 'idle',
  working: 'work',
  waiting: 'wait',
  paused: 'pause',
  stopped: 'stop',
};

export const AGENT_WORDS: Record<AgentState, string> = {
  idle: 'Idle',
  working: 'Working',
  waiting: 'Waiting for you',
  paused: 'Paused',
  stopped: 'Stopped',
};

export const FACT_MARK: Record<FactStatus | 'held', MarkKind> = {
  active: 'f-act',
  disputed: 'f-dis',
  superseded: 'f-sup',
  held: 'f-held',
};

export function Mark({
  kind,
  label,
  spin = false,
  size,
  style,
}: {
  kind: MarkKind;
  /** Read out by screen readers; leave empty when words sit beside it. */
  label?: string;
  /** The working mark's slow ring, for an agent at work now. */
  spin?: boolean;
  size?: number;
  style?: React.CSSProperties;
}) {
  const sized = size ? { width: size, height: size } : {};
  return (
    <span
      className={`gl ${kind}${spin && kind === 'work' ? ' spin' : ''}`}
      role={label ? 'img' : undefined}
      aria-label={label || undefined}
      aria-hidden={label ? undefined : true}
      style={{ ...sized, ...style }}
    />
  );
}
