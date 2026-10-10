"use client";

/**
 * The morning routine (designs: CcRoutines): work that starts on its own at
 * set times. Edit one, switch it, run it now, and see what fired and what it
 * cost. A routine a charter made is changed in its charter.
 */

import { useEffect, useMemo, useState } from "react";

import type { CcAgent } from "@/components/control/Agents";
import {
  DayChips,
  daysWords,
  Dialog,
  Empty,
  Field,
  Head,
  HistoryButton,
  Problem,
  Toggle,
  useLoad,
  useSave,
} from "@/components/control/kit";
import { api } from "@/lib/api";
import {
  agentName,
  departmentName,
  money,
  OWNER_TIME_ZONE,
} from "@/lib/format";

interface Run {
  task_id: string;
  at: string;
  status: string;
  cost_usd: number;
}

export interface Routine {
  id: string;
  key: string | null;
  name: string;
  agent: string;
  department: string | null;
  instructions: string;
  input: Record<string, unknown>;
  time: string;
  days: number[];
  timezone: string;
  enabled: boolean;
  grace_minutes: number;
  max_steps: number;
  max_tokens: number;
  runs: Run[];
}

const REFRESH = /^(trigger_|charter_|task_)/;

const ymd = new Intl.DateTimeFormat("en-CA", {
  timeZone: OWNER_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
const dayLabel = new Intl.DateTimeFormat("en-GB", {
  timeZone: OWNER_TIME_ZONE,
  weekday: "short",
  day: "numeric",
});

export function Routines() {
  const list = useLoad<Routine[]>("control/routines", REFRESH);
  const [chosen, setChosen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const save = useSave();
  const routines = list.data ?? [];
  useEffect(() => {
    if (!chosen && routines.length) setChosen(routines[0]!.id);
  }, [chosen, routines]);
  const picked = routines.find((r) => r.id === chosen) ?? null;

  async function switchOne(r: Routine, on: boolean) {
    await save.run(
      () => api.post(`control/routines/${r.id}/switch`, { on }),
      `${r.name} switched ${on ? "on" : "off"}`,
    );
    await list.reload();
  }

  async function runNow(r: Routine) {
    await save.run(
      () => api.post(`control/routines/${r.id}/run`),
      `${r.name} started; it shows on the brain`,
    );
    await list.reload();
  }

  return (
    <>
      <Head
        title="Morning routine"
        lede="Work that starts on its own at set times. New routines start switched off."
      >
        <HistoryButton prefix="trigger_" title="Morning routine" />
        <button
          type="button"
          className="btn pri"
          onClick={() => setAdding(true)}
        >
          New routine
        </button>
      </Head>
      <Problem text={list.error ?? save.error} />
      {list.data && !routines.length ? (
        <Empty title="No routines yet">
          Add one with New routine: an agent, a time and the days it runs.
        </Empty>
      ) : null}
      <div className="cc-split wide-left">
        <div style={{ display: "grid", gap: 14 }}>
          <div className="tbl scroll-x">
            <div
              className="th"
              style={{
                gridTemplateColumns: "58px minmax(160px,1fr) 110px 80px 80px",
              }}
            >
              <span>Time</span>
              <span>Routine</span>
              <span>Days</span>
              <span>Run</span>
              <span>On</span>
            </div>
            {routines.map((r) => (
              <div
                key={r.id}
                className={`row${r.id === chosen ? " sel" : ""}${r.enabled ? "" : " dim"}`}
                style={{
                  gridTemplateColumns: "58px minmax(160px,1fr) 110px 80px 80px",
                  paddingTop: 6,
                  paddingBottom: 6,
                }}
              >
                <b className="num">{r.time}</b>
                <button
                  type="button"
                  style={{ all: "unset", cursor: "pointer", minWidth: 0 }}
                  onClick={() => setChosen(r.id)}
                >
                  {r.name}
                  <span className="sub">
                    {agentName(r.agent)}
                    {r.department ? `, ${departmentName(r.department)}` : ""}
                  </span>
                </button>
                <span>{daysWords(r.days)}</span>
                <button
                  type="button"
                  className="btn sm glass"
                  disabled={save.busy}
                  onClick={() => void runNow(r)}
                >
                  Run now
                </button>
                <Toggle
                  on={r.enabled}
                  label={`${r.name} on or off`}
                  disabled={save.busy}
                  onChange={(on) => void switchOne(r, on)}
                />
              </div>
            ))}
          </div>
          {routines.length ? <Fired routines={routines} /> : null}
        </div>
        {picked ? (
          <Editor key={picked.id} routine={picked} onChanged={list.reload} />
        ) : null}
      </div>
      {adding ? (
        <NewRoutine
          onClose={() => setAdding(false)}
          onDone={async () => {
            setAdding(false);
            await list.reload();
          }}
        />
      ) : null}
    </>
  );
}

/** What fired in the last seven days: done, failed, paused or didn't run. */
function Fired({ routines }: { routines: Routine[] }) {
  const days = useMemo(() => {
    const out: Date[] = [];
    for (let i = 6; i >= 0; i--) out.push(new Date(Date.now() - i * 86400000));
    return out;
  }, []);
  return (
    <div className="cc-card">
      <div className="sech">
        <h3>What fired, last 7 days</h3>
        <span className="faint" style={{ fontSize: 12 }}>
          ● done · × failed · Ⅱ paused · □ didn&apos;t run
        </span>
      </div>
      <div className="scroll-x">
        <div
          className="grid"
          style={{ gridTemplateColumns: `minmax(130px,1fr) repeat(7, 64px)` }}
        >
          <span />
          {days.map((d) => (
            <span key={d.toISOString()} className="faint">
              {dayLabel.format(d)}
            </span>
          ))}
          {routines.map((r) => (
            <RoutineRow key={r.id} routine={r} days={days} />
          ))}
        </div>
      </div>
    </div>
  );
}

function RoutineRow({ routine, days }: { routine: Routine; days: Date[] }) {
  return (
    <>
      <span className="clamp2">
        <span className="num">{routine.time}</span> {routine.name}
      </span>
      {days.map((d) => {
        const key = ymd.format(d);
        const run = routine.runs.find(
          (x) => ymd.format(new Date(x.at)) === key,
        );
        const due = routine.days.includes(d.getDay());
        if (!run)
          return (
            <span
              key={key}
              className="faint"
              title={due ? "Didn't run" : "Not a routine day"}
            >
              {due ? "□" : "·"}
            </span>
          );
        const mark =
          run.status === "done"
            ? "●"
            : run.status === "failed" || run.status === "cancelled"
              ? "×"
              : run.status === "blocked"
                ? "Ⅱ"
                : "◌";
        return (
          <span
            key={key}
            className="num"
            title={`${run.status}, ${money(run.cost_usd)}`}
            style={{
              color: run.status === "failed" ? "var(--verm)" : undefined,
            }}
          >
            {mark} {money(run.cost_usd)}
          </span>
        );
      })}
    </>
  );
}

function Editor({
  routine,
  onChanged,
}: {
  routine: Routine;
  onChanged: () => Promise<void>;
}) {
  const [form, setForm] = useState({
    instructions: routine.instructions,
    time: routine.time,
    timezone: routine.timezone,
    days: routine.days,
    max_steps: String(routine.max_steps),
    max_tokens: String(routine.max_tokens),
  });
  const topics = Array.isArray(routine.input.topics)
    ? (routine.input.topics as string[])
    : null;
  const sources = Array.isArray(routine.input.sources)
    ? (routine.input.sources as string[])
    : null;
  const [list, setList] = useState({
    topics: topics ?? [],
    sources: sources ?? [],
  });
  const rest = Object.fromEntries(
    Object.entries(routine.input).filter(
      ([k]) => k !== "topics" && k !== "sources",
    ),
  );
  const [restText, setRestText] = useState(
    Object.keys(rest).length ? JSON.stringify(rest, null, 2) : "",
  );
  const save = useSave();
  const runs = routine.runs;
  const average = runs.length
    ? runs.reduce((s, r) => s + r.cost_usd, 0) / runs.length
    : 0;

  async function apply() {
    let extra: Record<string, unknown> = {};
    if (restText.trim()) {
      try {
        extra = JSON.parse(restText) as Record<string, unknown>;
      } catch {
        save.setError("The other input is not valid JSON.");
        return;
      }
    }
    const input = {
      ...extra,
      ...(topics ? { topics: list.topics } : {}),
      ...(sources ? { sources: list.sources } : {}),
    };
    const done = await save.run(
      () =>
        api.put(`control/routines/${routine.id}`, {
          instructions: form.instructions,
          time: form.time,
          timezone: form.timezone,
          days: form.days,
          max_steps: Number(form.max_steps),
          max_tokens: Number(form.max_tokens),
          input,
        }),
      routine.key
        ? `${routine.name} saved in its charter`
        : `${routine.name} saved`,
    );
    if (done) await onChanged();
  }

  return (
    <div className="cc-card">
      <div className="sech">
        <h2>{routine.name}</h2>
        <span className="faint" style={{ fontSize: 12 }}>
          {agentName(routine.agent)}
        </span>
      </div>
      <Field label="Instructions">
        <textarea
          className="inp"
          style={{ minHeight: 90 }}
          value={form.instructions}
          onChange={(e) => setForm({ ...form, instructions: e.target.value })}
        />
      </Field>
      <div className="fields">
        <Field label="Time (24-hour)">
          <input
            className="inp"
            value={form.time}
            onChange={(e) => setForm({ ...form, time: e.target.value })}
          />
        </Field>
        <Field label="Time zone">
          <input
            className="inp"
            value={form.timezone}
            onChange={(e) => setForm({ ...form, timezone: e.target.value })}
          />
        </Field>
      </div>
      <div className="fld">
        <span className="lbl">Days</span>
        <DayChips
          days={form.days}
          onChange={(days) => setForm({ ...form, days })}
        />
      </div>
      <div className="fields">
        <Field label="Max steps">
          <input
            className="inp"
            inputMode="numeric"
            value={form.max_steps}
            onChange={(e) => setForm({ ...form, max_steps: e.target.value })}
          />
        </Field>
        <Field label="Max tokens">
          <input
            className="inp"
            inputMode="numeric"
            value={form.max_tokens}
            onChange={(e) => setForm({ ...form, max_tokens: e.target.value })}
          />
        </Field>
      </div>
      {topics ? (
        <ListEdit
          label="Topics"
          items={list.topics}
          onChange={(items) => setList({ ...list, topics: items })}
          placeholder="Add a topic"
        />
      ) : null}
      {sources ? (
        <ListEdit
          label="Sources"
          items={list.sources}
          onChange={(items) => setList({ ...list, sources: items })}
          placeholder="https://…"
        />
      ) : null}
      <Field
        label={topics || sources ? "Other input (JSON)" : "Input (JSON)"}
        hint="Anything else the task carries, such as a mailing list"
      >
        <textarea
          className="inp code"
          style={{ minHeight: 70 }}
          value={restText}
          onChange={(e) => setRestText(e.target.value)}
        />
      </Field>
      <span className="cost">
        <span>
          Average <b>{money(average)}</b> a run
        </span>
        <span>
          Last runs <b>{runs.length}</b>
        </span>
      </span>
      <Problem text={save.error} />
      <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
        <button
          type="button"
          className="btn pri"
          disabled={save.busy || !form.days.length}
          onClick={() => void apply()}
        >
          Save
        </button>
      </div>
    </div>
  );
}

function ListEdit({
  label,
  items,
  onChange,
  placeholder,
}: {
  label: string;
  items: string[];
  onChange: (items: string[]) => void;
  placeholder: string;
}) {
  const [draft, setDraft] = useState("");
  return (
    <div className="fld">
      <span className="lbl">{label}</span>
      <div className="chipset">
        {items.map((item) => (
          <button
            key={item}
            type="button"
            className="chip"
            aria-label={`Remove ${item}`}
            title="Remove"
            onClick={() => onChange(items.filter((x) => x !== item))}
          >
            {item} ×
          </button>
        ))}
      </div>
      <form
        style={{ display: "flex", gap: 8 }}
        onSubmit={(e) => {
          e.preventDefault();
          const v = draft.trim();
          if (v && !items.includes(v)) onChange([...items, v]);
          setDraft("");
        }}
      >
        <input
          className="inp"
          value={draft}
          placeholder={placeholder}
          aria-label={`Add to ${label.toLowerCase()}`}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button type="submit" className="btn sm glass">
          Add
        </button>
      </form>
    </div>
  );
}

function NewRoutine({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const agents = useLoad<CcAgent[]>("control/agents");
  const [form, setForm] = useState({
    agent: "",
    title: "",
    instructions: "",
    time: "07:00",
    days: [1, 2, 3, 4, 5],
  });
  const save = useSave();
  const chosen = agents.data?.find((a) => a.name === form.agent);

  async function create() {
    if (!chosen?.department) {
      save.setError("Pick an agent that belongs to a department.");
      return;
    }
    const done = await save.run(
      () =>
        api.post("control/routines", {
          ...form,
          department: chosen.department,
        }),
      `${form.title} added to the ${departmentName(chosen.department)} charter, switched off`,
    );
    if (done) await onDone();
  }

  return (
    <Dialog title="New routine" onClose={onClose}>
      <div className="fields">
        <Field label="Agent">
          <select
            className="inp"
            value={form.agent}
            onChange={(e) => setForm({ ...form, agent: e.target.value })}
          >
            <option value="">Pick one</option>
            {(agents.data ?? []).map((a) => (
              <option key={a.name} value={a.name}>
                {agentName(a.name)} · {departmentName(a.department)}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Title">
          <input
            className="inp"
            value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
          />
        </Field>
        <Field label="Time (24-hour)">
          <input
            className="inp"
            value={form.time}
            onChange={(e) => setForm({ ...form, time: e.target.value })}
          />
        </Field>
        <Field label="Instructions" wide>
          <textarea
            className="inp"
            value={form.instructions}
            onChange={(e) => setForm({ ...form, instructions: e.target.value })}
          />
        </Field>
      </div>
      <div className="fld">
        <span className="lbl">Days</span>
        <DayChips
          days={form.days}
          onChange={(days) => setForm({ ...form, days })}
        />
      </div>
      <Problem text={save.error} />
      <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
        <button
          type="button"
          className="btn pri"
          disabled={
            save.busy ||
            !form.agent ||
            form.title.length < 3 ||
            !form.days.length
          }
          onClick={() => void create()}
        >
          Add, switched off
        </button>
      </div>
    </Dialog>
  );
}
