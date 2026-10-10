"use client";

/**
 * The Control Center's shared patterns (designs: CcPatterns.dc.html), each
 * defined once: loading, the toggle, tags, fields, saving with a short
 * confirmation, the history drawer, a dialog, comparing two versions, the
 * locked rule and cost beside a setting.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { Icon } from "@/components/Icon";
import { api } from "@/lib/api";
import { useCompany } from "@/lib/company";
import { agentName, departmentName, money, when } from "@/lib/format";

// --- Loading -----------------------------------------------------------------

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  reload: () => Promise<void>;
}

/** GET a path; read again when a live event of the given kinds arrives. */
export function useLoad<T>(path: string | null, refreshOn?: RegExp): Loaded<T> {
  const { onEvent } = useCompany();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const live = useRef(path);
  live.current = path;

  const reload = useCallback(async () => {
    if (!path) return;
    try {
      const got = await api.get<T>(path);
      if (live.current === path) {
        setData(got);
        setError(null);
      }
    } catch (err) {
      if (live.current === path)
        setError(err instanceof Error ? err.message : String(err));
    }
  }, [path]);

  useEffect(() => {
    setData(null);
    void reload();
  }, [reload]);

  useEffect(() => {
    if (!refreshOn) return;
    let soon: ReturnType<typeof setTimeout> | undefined;
    const stop = onEvent((event) => {
      if (!refreshOn.test(event.type)) return;
      clearTimeout(soon);
      soon = setTimeout(() => void reload(), 700);
    });
    return () => {
      clearTimeout(soon);
      stop();
    };
  }, [onEvent, reload, refreshOn]);

  return { data, error, reload };
}

// --- Saving, with a short confirmation ----------------------------------------

const ToastContext = createContext<(text: string) => void>(() => undefined);

export function ToastHost({ children }: { children: ReactNode }) {
  const [text, setText] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const show = useCallback((next: string) => {
    setText(next);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setText(null), 3200);
  }, []);
  return (
    <ToastContext.Provider value={show}>
      {children}
      {text ? (
        <div className="glass toast" role="status">
          {text}
        </div>
      ) : null}
    </ToastContext.Provider>
  );
}

export function useToast(): (text: string) => void {
  return useContext(ToastContext);
}

/** Run a change: busy while it runs, its error kept, a toast when it worked. */
export function useSave() {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(
    async <T,>(
      work: () => Promise<T>,
      done?: string,
    ): Promise<T | undefined> => {
      setBusy(true);
      setError(null);
      try {
        const out = await work();
        if (done) toast(done);
        return out;
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [toast],
  );
  return { busy, error, run, setError };
}

// --- Small parts --------------------------------------------------------------

export function Toggle({
  on,
  onChange,
  label,
  disabled,
  words = ["On", "Off"],
}: {
  on: boolean;
  onChange: (on: boolean) => void;
  label: string;
  disabled?: boolean;
  words?: [string, string];
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className="tog"
      disabled={disabled}
      onClick={() => onChange(!on)}
    >
      <span className="tr" aria-hidden="true" />
      {on ? words[0] : words[1]}
    </button>
  );
}

export function Tag({
  kind,
  children,
}: {
  kind?: string;
  children: ReactNode;
}) {
  return <span className={`tag${kind ? ` ${kind}` : ""}`}>{children}</span>;
}

export function Field({
  label,
  hint,
  children,
  wide,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <label className="fld" style={wide ? { gridColumn: "1 / -1" } : undefined}>
      <span className="lbl">{label}</span>
      {children}
      {hint ? <span className="sub">{hint}</span> : null}
    </label>
  );
}

export function Lock({ children }: { children: ReactNode }) {
  return (
    <div className="lock">
      <Icon name="lock" size={14} />
      <span>{children}</span>
    </div>
  );
}

export function Problem({ text }: { text: string | null }) {
  return text ? (
    <p className="err" role="alert">
      <Icon name="warn" size={14} />
      {text}
    </p>
  ) : null;
}

/** Spend beside a setting that spends money. */
export function Cost({
  today,
  week,
  budget,
}: {
  today?: number;
  week?: number;
  budget?: number;
}) {
  return (
    <span className="cost">
      {today !== undefined ? (
        <span>
          Today <b>{money(today)}</b>
        </span>
      ) : null}
      {week !== undefined ? (
        <span>
          7 days <b>{money(week)}</b>
        </span>
      ) : null}
      {budget !== undefined ? (
        <span>
          Budget <b>{money(budget)}</b> a day
        </span>
      ) : null}
    </span>
  );
}

export function Meter({ value, of }: { value: number; of: number }) {
  const share = of > 0 ? Math.min(100, (value / of) * 100) : 0;
  return (
    <span
      className={`meter${of > 0 && value > of ? " over" : ""}`}
      role="img"
      aria-label={`${money(value)} of ${money(of)}`}
    >
      <i style={{ width: `${share}%` }} />
    </span>
  );
}

export function Head({
  title,
  lede,
  children,
}: {
  title: string;
  lede: string;
  children?: ReactNode;
}) {
  return (
    <header className="cc-head">
      <div>
        <h1 className="disp">{title}</h1>
        <p>{lede}</p>
      </div>
      {children ? <div className="cc-actions">{children}</div> : null}
    </header>
  );
}

export function Empty({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty-state">
      <b style={{ color: "var(--ink)" }}>{title}</b>
      <span>{children}</span>
    </div>
  );
}

export const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function daysWords(days: number[]): string {
  const set = [...new Set(days)].sort();
  if (set.length === 7) return "Every day";
  if (set.join() === "1,2,3,4,5") return "Mon to Fri";
  if (set.join() === "1,2,3,4,5,6") return "Mon to Sat";
  if (set.join() === "0,1,2,3,4,5") return "Sun to Fri";
  return set.map((d) => DAYS[d]).join(", ");
}

export function DayChips({
  days,
  onChange,
}: {
  days: number[];
  onChange: (days: number[]) => void;
}) {
  // Monday first, as the owner reads a week.
  const order = [1, 2, 3, 4, 5, 6, 0];
  return (
    <div className="chipset" role="group" aria-label="Days">
      {order.map((d) => (
        <button
          key={d}
          type="button"
          className="day"
          aria-pressed={days.includes(d)}
          aria-label={DAYS[d]}
          onClick={() =>
            onChange(
              days.includes(d)
                ? days.filter((x) => x !== d)
                : [...days, d].sort(),
            )
          }
        >
          {DAYS[d]![0]}
        </button>
      ))}
    </div>
  );
}

// --- A dialog: preview before apply, confirmations --------------------------

export function Dialog({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      className="cc-dialog-back"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className="glass cc-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="sech">
          <h2 className="disp" style={{ fontSize: 22, margin: 0 }}>
            {title}
          </h2>
          <button
            type="button"
            className="btn sm glass ibtn"
            aria-label="Close"
            onClick={onClose}
          >
            <Icon name="close" size={14} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** A charter apply's summary as the owner reads it. */
export function ApplySummary({ report }: { report: ApplyReport }) {
  const lines = [
    ...report.created.map(
      (x) =>
        `Creates ${x}${x.startsWith("agent") || x.startsWith("trigger") ? " (switched off)" : ""}`,
    ),
    ...report.updated.map((x) => `Updates ${x}`),
    ...report.switched_off.map((x) => `Switches off ${x}`),
  ];
  return lines.length ? (
    <ul>
      {lines.map((l) => (
        <li key={l}>{l.replace(/^(\w+) trigger /, "$1 routine ")}</li>
      ))}
    </ul>
  ) : (
    <p className="muted" style={{ margin: 0 }}>
      Nothing changes: everything already matches.
    </p>
  );
}

export interface ApplyReport {
  department: string;
  version: number;
  created: string[];
  updated: string[];
  unchanged: string[];
  switched_off: string[];
}

// --- Comparing two versions ---------------------------------------------------

/** Line-by-line comparison: removed lines on the left, added on the right. */
export function Compare({
  left,
  right,
  leftLabel,
  rightLabel,
}: {
  left: string;
  right: string;
  leftLabel: string;
  rightLabel: string;
}) {
  const a = left.split("\n");
  const b = right.split("\n");
  // Longest common subsequence of lines: small texts, so the table is cheap.
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () =>
    new Array<number>(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      lcs[i]![j] =
        a[i] === b[j]
          ? lcs[i + 1]![j + 1]! + 1
          : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
  const keepA = new Set<number>();
  const keepB = new Set<number>();
  for (let i = 0, j = 0; i < n && j < m; ) {
    if (a[i] === b[j]) {
      keepA.add(i++);
      keepB.add(j++);
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) i++;
    else j++;
  }
  return (
    <div className="diff">
      <div className="fld">
        <span className="lbl">{leftLabel}</span>
        <pre>
          {a.map((line, i) =>
            keepA.has(i) ? (
              <span key={i}>
                {line}
                {"\n"}
              </span>
            ) : (
              <del key={i}>
                {line}
                {"\n"}
              </del>
            ),
          )}
        </pre>
      </div>
      <div className="fld">
        <span className="lbl">{rightLabel}</span>
        <pre>
          {b.map((line, i) =>
            keepB.has(i) ? (
              <span key={i}>
                {line}
                {"\n"}
              </span>
            ) : (
              <ins key={i}>
                {line}
                {"\n"}
              </ins>
            ),
          )}
        </pre>
      </div>
    </div>
  );
}

// --- The change log, in plain words ------------------------------------------

export interface LogEvent {
  id: string;
  type: string;
  payload: Record<string, unknown>;
  at: string;
  agent: string | null;
}

const str = (v: unknown) => (v === null || v === undefined ? "" : String(v));

/** One change as a sentence: "Research budget $0.25 → $0.50". */
export function describe(event: LogEvent): string {
  const p = event.payload ?? {};
  const t = event.type;
  const was = (p.was ?? {}) as Record<string, unknown>;
  switch (true) {
    case t === "department_updated":
      if (
        was.daily_budget_usd !== undefined &&
        was.daily_budget_usd !== p.daily_budget_usd
      )
        return `${departmentName(str(p.department))} budget ${money(Number(was.daily_budget_usd))} → ${money(Number(p.daily_budget_usd))} a day`;
      return `${departmentName(str(p.department))} switched ${p.enabled ? "on" : "off"}`;
    case t === "department_created":
      return `${departmentName(str(p.department))} department created`;
    case t === "charter_activated":
      return `${departmentName(str(p.department))} charter v${str(p.version)} is live${p.note ? `: ${str(p.note)}` : ""}`;
    case t === "charter_applied": {
      const changed = [
        ...((p.created as string[]) ?? []),
        ...((p.updated as string[]) ?? []),
      ];
      return `${departmentName(str(p.department))} charter v${str(p.version)} applied${changed.length ? `: ${changed.join(", ")}` : ", nothing to change"}`;
    }
    case t === "agent_prompt_activated":
      return `${agentName(event.agent ?? str(p.agent))} ${str(p.slot)} prompt v${str(p.version)} is live`.trim();
    case t === "agent_prompt_deactivated":
      return `${agentName(event.agent ?? str(p.agent))} ${str(p.slot)} prompt v${str(p.version)} retired`.trim();
    case t === "agent_created":
      return `Agent ${agentName(str(p.name))} created${p.enabled ? "" : ", switched off"}`;
    case t === "agent_enabled":
      return `${agentName(str(p.name))} switched on`;
    case t === "agent_disabled":
      return `${agentName(str(p.name))} switched off`;
    case t.startsWith("agent_"):
      return `${agentName(str(p.name))} changed${p.note ? `: ${str(p.note)}` : ""}`;
    case t === "trigger_created":
      return `Routine "${str(p.name)}" created${p.enabled ? "" : ", switched off"}`;
    case t === "trigger_updated":
      return `Routine "${str(p.name)}" now ${str(p.time_of_day).slice(0, 5)}, ${daysWords((p.days_of_week as number[]) ?? [])}${p.enabled ? "" : ", off"}`;
    case t === "trigger_deleted":
      return `Routine "${str(p.name)}" removed`;
    case t === "tool_created":
    case t === "tool_updated":
      return `Tool ${str(p.tool).replace(/_/g, " ")}: ${str(p.risk_class)}, ${p.approval === "auto" ? "runs by level" : "asks you"}, ${p.enabled ? "on" : "off"}`;
    case t.startsWith("mcp_server"):
      return `MCP server ${str(p.server)}: ${str(p.status)}`;
    case t === "mcp_tools_listed":
      return `MCP server ${str(p.server)} listed ${((p.added as unknown[]) ?? []).length} new tools`;
    case t.startsWith("mcp_tool"):
      return `MCP tool ${str(p.tool ?? p.name).replace(/_/g, " ")} changed`;
    case t === "judge_gate_activated":
      return `Judge gate ${str(p.gate).replace(/_/g, " ")} v${str(p.version)} is live, ${p.enabled ? "on" : "off"}, fails ${str(p.fail_mode)}`;
    case t === "judge_gate_deactivated":
      return `Judge gate ${str(p.gate).replace(/_/g, " ")} v${str(p.version)} retired`;
    case t.startsWith("judge_question"):
      return `Judge question ${str(p.gate)}/${str(p.key)} v${str(p.version)} ${t.endsWith("activated") ? "is live" : "retired"}`;
    case t.startsWith("autonomy_"):
      return `Ladder: ${str(p.level)} × ${str(p.risk_class)} → ${modeWords(str(p.mode))}`;
    case t.startsWith("delegation_limits"):
      return "Safety limits changed";
    case t === "model_tier_changed":
      return `${str(p.tier)} tier${p.department_id ? " (one department)" : ""}: ${str(p.from) || "default"} → ${str(p.to) || "default"}`;
    case t === "model_price_changed":
      return `Price for ${str(p.model)} recorded`;
    case t.startsWith("mailing_list"):
      return `Mailing list ${str(p.list)}: ${((p.recipients as unknown[]) ?? []).length} recipients, ${p.send_without_approval ? "sends on its own" : "waits for approval"}`;
    case t === "document_added":
      return `Document "${str(p.title)}" added (${str(p.scope)}), ${str(p.status)}`;
    case t.startsWith("document_"):
      return `Document "${str(p.title)}" changed`;
    case t === "rule_retired":
      return `Rule retired: ${str(p.rule)}`;
    case t === "approval_decided":
      return `You ${p.decision === "approve" ? "approved" : "rejected"} ${str(p.action_key).replace(/[:_]/g, " ")}${p.note ? `: ${str(p.note)}` : ""}`;
    case t === "paused":
      return "Everything paused";
    case t === "unpaused":
      return "Pause lifted";
    case t === "killed":
      return "Kill: all unfinished work cancelled";
    case t === "fact_visibility_changed":
      return `Fact made ${str(p.to)}: ${str(p.claim)}`;
    case t.startsWith("routine_request"):
      return `Asked for tomorrow: ${str(p.request)}`;
    default:
      return t.replace(/_/g, " ");
  }
}

export function modeWords(mode: string): string {
  return mode === "run"
    ? "runs"
    : mode === "gate"
      ? "Jev checks"
      : mode === "hold"
        ? "asks you"
        : mode;
}

/** Who made a change: the owner, or the system. */
function by(event: LogEvent): string {
  const p = event.payload ?? {};
  if (p.changed_by || p.decided_by) return "you";
  if (p.db_role === "postgres" || p.db_role === "service_role") return "set up";
  return "Pantheon";
}

export function LogList({ events }: { events: LogEvent[] }) {
  if (!events.length)
    return (
      <p className="faint" style={{ fontSize: 13 }}>
        No changes yet.
      </p>
    );
  return (
    <div>
      {events.map((e) => (
        <div key={e.id} className="ev">
          <span className="dot" aria-hidden="true" />
          <span>
            {describe(e)}
            <span className="w">
              {by(e)}, {when(e.at)}
            </span>
          </span>
        </div>
      ))}
    </div>
  );
}

/** "History": the audit events for what this screen changes. */
export function HistoryButton({
  prefix,
  title,
}: {
  prefix: string;
  title: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="btn glass"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Icon name="history" size={14} />
        History
      </button>
      {open ? (
        <HistoryDrawer
          prefix={prefix}
          title={title}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}

function HistoryDrawer({
  prefix,
  title,
  onClose,
}: {
  prefix: string;
  title: string;
  onClose: () => void;
}) {
  const log = useLoad<LogEvent[]>(
    `control/log?limit=60&prefix=${encodeURIComponent(prefix)}`,
    /./,
  );
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <aside className="glass cc-drawer" aria-label={`${title} history`}>
      <div className="sech">
        <h2 className="disp" style={{ fontSize: 20, margin: 0 }}>
          {title} history
        </h2>
        <button
          type="button"
          className="btn sm glass ibtn"
          aria-label="Close history"
          onClick={onClose}
        >
          <Icon name="close" size={14} />
        </button>
      </div>
      <Problem text={log.error} />
      {log.data ? (
        <LogList events={log.data} />
      ) : (
        <p className="faint">Loading</p>
      )}
    </aside>
  );
}
