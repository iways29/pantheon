"use client";

/**
 * Departments and their charters (designs: CcDepts, CcDeptsPreview,
 * CcDeptsHistory). A charter is the department as data: change it, see what
 * applying it would do, then apply. Every version is kept; any can be made
 * live again.
 */

import { useEffect, useMemo, useState } from "react";

import {
  ApplySummary,
  Compare,
  Cost,
  daysWords,
  Dialog,
  Empty,
  Field,
  Head,
  HistoryButton,
  Lock,
  Meter,
  Problem,
  Tag,
  Toggle,
  useLoad,
  useSave,
  type ApplyReport,
} from "@/components/control/kit";
import { api } from "@/lib/api";
import { agentName, departmentName, money, when } from "@/lib/format";

interface Dept {
  name: string;
  enabled: boolean;
  daily_budget_usd: number;
  head: string | null;
  agents: number;
  agents_on: number;
  spend_today_usd: number;
  spend_7d_usd: number;
  charter_version: number | null;
  draft: boolean;
  purpose: string | null;
}

interface Plan {
  name: string;
  role: string;
  tier: string;
  runner: string;
  role_type: string | null;
  allowed_tools: string[];
  prompts: Record<string, string>;
}

interface CharterDoc {
  purpose: string;
  daily_budget_usd: string;
  autonomy_level: string;
  head: Plan;
  workers: Plan[];
  routine: {
    key: string;
    title: string;
    time: string;
    days: number[];
    agent: string;
  }[];
  approval_rules: string[];
  gates: string[];
  metrics: string[];
  draft: boolean;
}

interface Version {
  version: number;
  active: boolean;
  note: string | null;
  at: string;
  charter: CharterDoc;
}

interface Report {
  error?: string;
  days?: {
    day: string;
    ok: boolean;
    spend_usd: string;
    routine: { status: string; tree_cost_usd: string }[];
  }[];
  mornings_in_a_row?: number;
  done?: boolean;
}

const REFRESH = /^(charter_|department_|agent_|trigger_)/;

export function Departments() {
  const list = useLoad<Dept[]>("control/departments", REFRESH);
  const [chosen, setChosen] = useState<string | null>(null);
  const save = useSave();
  const depts = list.data ?? [];
  useEffect(() => {
    if (!chosen && depts.length)
      setChosen(
        depts.find((d) => d.name === "research")?.name ?? depts[0]!.name,
      );
  }, [chosen, depts]);
  const budgets = depts.reduce(
    (sum, d) => sum + (d.agents_on ? d.daily_budget_usd : 0),
    0,
  );
  const today = depts.reduce((sum, d) => sum + d.spend_today_usd, 0);

  async function switchDept(d: Dept, on: boolean) {
    await save.run(
      () => api.post(`departments/${d.name}/enable`, { on }),
      `${departmentName(d.name)} switched ${on ? "on" : "off"}, with its agents and routines`,
    );
    await list.reload();
  }

  return (
    <>
      <Head
        title="Departments"
        lede="Each department runs on a charter. Change it, preview what applying does, then apply."
      >
        <HistoryButton prefix="charter_,department_" title="Departments" />
      </Head>
      <Problem text={list.error ?? save.error} />
      <div className="cc-split">
        <div style={{ display: "grid", gap: 14 }}>
          <div className="tbl" role="table" aria-label="Departments">
            <div
              className="th"
              role="row"
              style={{ gridTemplateColumns: "minmax(0,1fr) 70px 80px" }}
            >
              <span>Department</span>
              <span>Today</span>
              <span>On</span>
            </div>
            {depts.map((d) => (
              <div
                key={d.name}
                role="row"
                className={`row${d.name === chosen ? " sel" : ""}`}
                style={{
                  gridTemplateColumns: "minmax(0,1fr) 70px 80px",
                  paddingTop: 8,
                  paddingBottom: 8,
                }}
              >
                <button
                  type="button"
                  className="text-btn"
                  style={{ all: "unset", cursor: "pointer", minWidth: 0 }}
                  onClick={() => setChosen(d.name)}
                  aria-label={`Open ${departmentName(d.name)}`}
                >
                  <span
                    style={{
                      display: "flex",
                      gap: 8,
                      alignItems: "center",
                      flexWrap: "wrap",
                    }}
                  >
                    {departmentName(d.name)}
                    {d.charter_version ? (
                      <Tag kind={d.draft ? "draft" : "live"}>
                        {d.draft ? "Draft" : "Live"} v{d.charter_version}
                      </Tag>
                    ) : (
                      <Tag kind="off">No charter</Tag>
                    )}
                  </span>
                  <span className="sub">
                    {d.head ? agentName(d.head) : "No head yet"}, {d.agents}{" "}
                    agents, {money(d.daily_budget_usd)} a day
                  </span>
                </button>
                <span className="num">{money(d.spend_today_usd)}</span>
                <Toggle
                  on={d.agents_on > 0}
                  label={`${departmentName(d.name)} on or off`}
                  disabled={save.busy || !d.charter_version}
                  onChange={(on) => void switchDept(d, on)}
                />
              </div>
            ))}
          </div>
          {depts.length ? (
            <div className="cc-card" style={{ gap: 8 }}>
              <div className="sech">
                <span>Budgets set</span>
                <span className="num">
                  <b>{money(budgets)}</b> a day · {money(today)} spent today
                </span>
              </div>
              <Meter value={today} of={budgets} />
              <span className="sub">
                Switching a department on also switches on its agents and
                routines; new ones start off.
              </span>
            </div>
          ) : list.data ? (
            <Empty title="No departments yet">
              Departments come from charters; seed the starter charters first.
            </Empty>
          ) : null}
          {chosen ? <DeptReport department={chosen} /> : null}
        </div>
        {chosen ? (
          <Charter
            key={chosen}
            dept={depts.find((d) => d.name === chosen) ?? null}
            name={chosen}
            onChanged={list.reload}
          />
        ) : null}
      </div>
    </>
  );
}

function DeptReport({ department }: { department: string }) {
  const [days, setDays] = useState(7);
  const report = useLoad<Report>(
    `departments/${department}/report?days=${days}`,
  );
  const r = report.data;
  const met = r?.days?.filter((d) => d.ok).length ?? 0;
  const runs = r?.days?.flatMap((d) => d.routine) ?? [];
  const cost = runs.reduce((s, x) => s + Number(x.tree_cost_usd), 0);
  return (
    <div className="cc-card">
      <div className="sech">
        <h3>{departmentName(department)} report</h3>
        <div className="seg" role="group" aria-label="Days">
          {[7, 14, 30].map((n) => (
            <button
              key={n}
              type="button"
              aria-pressed={days === n}
              onClick={() => setDays(n)}
            >
              {n} days
            </button>
          ))}
        </div>
      </div>
      {r?.error ? (
        <span className="muted">{r.error}</span>
      ) : r?.days ? (
        <>
          <div>
            <span className="disp" style={{ fontSize: 28 }}>
              {met} of {r.days.length}
            </span>{" "}
            <span className="muted">days ran their morning on budget</span>
          </div>
          <div className="chipset" aria-label="Each day">
            {r.days.map((d) => (
              <span
                key={d.day}
                className="tag"
                title={`${d.day}: ${d.ok ? "done" : "not done"}, ${money(Number(d.spend_usd))}`}
                aria-label={`${d.day} ${d.ok ? "done" : "not done"}`}
              >
                {d.ok ? "●" : "×"}
              </span>
            ))}
          </div>
          <span className="cost">
            <span>
              Cost per run <b>{money(runs.length ? cost / runs.length : 0)}</b>
            </span>
            <span>
              Runs <b>{runs.length}</b>
            </span>
            <span>
              In a row <b>{r.mornings_in_a_row ?? 0}</b>
            </span>
          </span>
        </>
      ) : (
        <span className="faint">{report.error ?? "Loading"}</span>
      )}
    </div>
  );
}

function Charter({
  name,
  dept,
  onChanged,
}: {
  name: string;
  dept: Dept | null;
  onChanged: () => Promise<void>;
}) {
  const versions = useLoad<Version[]>(
    `control/departments/${name}/charters`,
    REFRESH,
  );
  const live = versions.data?.find((v) => v.active) ?? versions.data?.[0];
  const save = useSave();
  const [budget, setBudget] = useState("");
  const [editing, setEditing] = useState(false);
  const [history, setHistory] = useState(false);
  useEffect(() => {
    if (live) setBudget(String(live.charter.daily_budget_usd));
  }, [live?.version]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!live) {
    return (
      <div className="cc-card">
        <h2>{departmentName(name)} charter</h2>
        {versions.error ? (
          <Problem text={versions.error} />
        ) : (
          <span className="faint">Loading</span>
        )}
      </div>
    );
  }
  const c = live.charter;
  const people = [c.head, ...c.workers];

  async function saveBudget() {
    const value = Number(budget);
    if (!Number.isFinite(value) || value < 0 || value > 100) {
      save.setError("A daily budget is between $0 and $100.");
      return;
    }
    await save.run(
      () =>
        api.post(`control/departments/${name}/budget`, {
          daily_budget_usd: value,
        }),
      `${departmentName(name)} budget ${money(value)} a day, as charter v${live!.version + 1}`,
    );
    await versions.reload();
    await onChanged();
  }

  return (
    <div className="cc-card">
      <div className="sech">
        <h2>{departmentName(name)} charter</h2>
        <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Tag kind={c.draft ? "draft" : "live"}>
            {c.draft ? "Draft" : "Live"} v{live.version}
          </Tag>
          <span className="faint" style={{ fontSize: 12 }}>
            since {when(live.at)}
          </span>
        </span>
      </div>
      <div
        className="fields"
        style={{ gridTemplateColumns: "minmax(0, 1.4fr) minmax(0, 1fr)" }}
      >
        <Field label="Purpose">
          <p style={{ margin: 0, lineHeight: 1.5, fontSize: 13.5 }}>
            {c.purpose}
          </p>
        </Field>
        <div className="fld">
          <span className="lbl">Daily budget</span>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              className="inp"
              inputMode="decimal"
              aria-label="Daily budget in dollars"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              style={{ maxWidth: 110 }}
            />
            <button
              type="button"
              className="btn sm glass"
              disabled={
                save.busy || Number(budget) === Number(c.daily_budget_usd)
              }
              onClick={() => void saveBudget()}
            >
              Save
            </button>
          </div>
          {dept ? (
            <Cost today={dept.spend_today_usd} week={dept.spend_7d_usd} />
          ) : null}
          <span className="sub">Default autonomy {c.autonomy_level}</span>
        </div>
      </div>
      <Problem text={save.error ?? versions.error} />

      <h3>People</h3>
      <div className="tbl scroll-x">
        <div
          className="th"
          style={{
            gridTemplateColumns:
              "minmax(120px,1fr) 90px 80px 80px minmax(120px,1.4fr) 60px",
          }}
        >
          <span>Name</span>
          <span>Role</span>
          <span>Runner</span>
          <span>Tier</span>
          <span>Tools</span>
          <span>Prompts</span>
        </div>
        {people.map((p, i) => (
          <div
            key={p.name}
            className="row"
            style={{
              gridTemplateColumns:
                "minmax(120px,1fr) 90px 80px 80px minmax(120px,1.4fr) 60px",
            }}
          >
            <span>{agentName(p.name)}</span>
            <span>
              {p.role_type === "chief_of_staff"
                ? "Chief of Staff"
                : i === 0
                  ? "Head"
                  : "Worker"}
            </span>
            <span>{p.runner}</span>
            <span>{p.tier}</span>
            <span className="sub clamp2" style={{ margin: 0 }}>
              {p.allowed_tools.map((t) => t.replace(/_/g, " ")).join(", ") ||
                "None"}
            </span>
            <span className="num">{Object.keys(p.prompts ?? {}).length}</span>
          </div>
        ))}
      </div>

      <div
        className="fields"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}
      >
        <div className="fld">
          <span className="lbl">Morning routine</span>
          {c.routine.length ? (
            c.routine.map((r) => (
              <span key={r.key} style={{ fontSize: 13 }}>
                <b className="num">{r.time}</b> {r.title}
                <span className="sub">{daysWords(r.days)}</span>
              </span>
            ))
          ) : (
            <span className="faint">None</span>
          )}
        </div>
        <div className="fld">
          <span className="lbl">Approval rules</span>
          {c.approval_rules.length ? (
            c.approval_rules.map((r) => (
              <span key={r} style={{ fontSize: 13, lineHeight: 1.4 }}>
                {r}
              </span>
            ))
          ) : (
            <span className="faint">None written</span>
          )}
        </div>
        <div className="fld">
          <span className="lbl">Judge checks</span>
          <div className="chipset">
            {c.gates.length ? (
              c.gates.map((g) => (
                <Tag key={g} kind="ok">
                  {g.replace(/_/g, " ")}
                </Tag>
              ))
            ) : (
              <span className="faint">None</span>
            )}
          </div>
        </div>
      </div>
      {c.metrics.length ? (
        <div className="fld">
          <span className="lbl">Done when</span>
          {c.metrics.map((m) => (
            <span key={m} style={{ fontSize: 13 }}>
              {m}
            </span>
          ))}
        </div>
      ) : null}

      <div
        className="cc-actions"
        style={{
          justifyContent: "flex-end",
          borderTop: "1px solid var(--line)",
          paddingTop: 12,
        }}
      >
        <button
          type="button"
          className="btn glass"
          onClick={() => setHistory(true)}
        >
          Versions ({versions.data?.length ?? 0})
        </button>
        <button
          type="button"
          className="btn pri"
          onClick={() => setEditing(true)}
        >
          Edit charter
        </button>
      </div>
      {editing ? (
        <CharterEditor
          name={name}
          live={live}
          onClose={() => setEditing(false)}
          onDone={async () => {
            setEditing(false);
            await versions.reload();
            await onChanged();
          }}
        />
      ) : null}
      {history && versions.data ? (
        <Versions
          name={name}
          versions={versions.data}
          onClose={() => setHistory(false)}
          onDone={async () => {
            await versions.reload();
            await onChanged();
          }}
        />
      ) : null}
    </div>
  );
}

/** The whole charter as JSON: change it, preview, then publish and apply. */
function CharterEditor({
  name,
  live,
  onClose,
  onDone,
}: {
  name: string;
  live: Version;
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const start = useMemo(() => JSON.stringify(live.charter, null, 2), [live]);
  const [text, setText] = useState(start);
  const [note, setNote] = useState("");
  const [preview, setPreview] = useState<ApplyReport | null>(null);
  const save = useSave();

  function parsed(): unknown {
    try {
      return JSON.parse(text);
    } catch (error) {
      save.setError(
        `Not valid JSON: ${error instanceof Error ? error.message : String(error)}`,
      );
      return null;
    }
  }

  async function check() {
    const charter = parsed();
    if (!charter) return;
    const report = await save.run(() =>
      api.post<ApplyReport>(`control/departments/${name}/preview`, { charter }),
    );
    if (report) setPreview(report);
  }

  async function publish(asDraft: boolean) {
    const charter = parsed() as Record<string, unknown> | null;
    if (!charter) return;
    const done = await save.run(
      async () => {
        const made = await api.post<{ version: number }>(
          `departments/${name}/charter`,
          {
            charter: { ...charter, draft: asDraft },
            note: note || null,
          },
        );
        if (!asDraft) await api.post(`departments/${name}/apply`);
        return made;
      },
      asDraft
        ? "Saved as a draft"
        : `${departmentName(name)} charter published and applied`,
    );
    if (done) await onDone();
  }

  return (
    <Dialog
      title={`Edit the ${departmentName(name)} charter`}
      onClose={onClose}
    >
      <p className="muted" style={{ margin: 0, fontSize: 13 }}>
        The whole charter: purpose, people and their prompts, the morning
        routine, rules, checks and budget. Preview shows what applying it would
        do before anything changes.
      </p>
      <textarea
        className="inp code"
        style={{ minHeight: 360 }}
        value={text}
        spellCheck={false}
        aria-label="Charter"
        onChange={(e) => {
          setText(e.target.value);
          setPreview(null);
        }}
      />
      <Field label="Note (why)">
        <input
          className="inp"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          maxLength={300}
        />
      </Field>
      <Problem text={save.error} />
      {preview ? (
        <div className="cc-card" style={{ padding: 14 }}>
          <b>Applying this would:</b>
          <ApplySummary report={preview} />
        </div>
      ) : null}
      <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
        <button
          type="button"
          className="btn glass"
          disabled={save.busy}
          onClick={() => void publish(true)}
        >
          Save as draft
        </button>
        {preview ? (
          <button
            type="button"
            className="btn pri"
            disabled={save.busy}
            onClick={() => void publish(false)}
          >
            Publish and apply
          </button>
        ) : (
          <button
            type="button"
            className="btn pri"
            disabled={save.busy || text === start}
            onClick={() => void check()}
          >
            Preview
          </button>
        )}
      </div>
    </Dialog>
  );
}

function Versions({
  name,
  versions,
  onClose,
  onDone,
}: {
  name: string;
  versions: Version[];
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const live = versions.find((v) => v.active) ?? versions[0]!;
  const [other, setOther] = useState(
    versions.find((v) => !v.active)?.version ?? live.version,
  );
  const picked = versions.find((v) => v.version === other) ?? live;
  const save = useSave();
  return (
    <Dialog
      title={`${departmentName(name)} charter versions`}
      onClose={onClose}
    >
      <div className="tbl">
        {versions.map((v) => (
          <button
            key={v.version}
            type="button"
            className={`row${v.version === other ? " sel" : ""}`}
            style={{ gridTemplateColumns: "50px minmax(0,1fr) 90px" }}
            onClick={() => setOther(v.version)}
          >
            <span className="num">v{v.version}</span>
            <span className="clamp2">{v.note ?? "No note"}</span>
            <span className="sub" style={{ margin: 0 }}>
              {v.active ? "Live" : when(v.at)}
            </span>
          </button>
        ))}
      </div>
      {picked.version !== live.version ? (
        <>
          <Compare
            left={JSON.stringify(picked.charter, null, 2)}
            right={JSON.stringify(live.charter, null, 2)}
            leftLabel={`v${picked.version}`}
            rightLabel={`v${live.version}, live`}
          />
          <Problem text={save.error} />
          <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
            <button
              type="button"
              className="btn pri"
              disabled={save.busy}
              onClick={async () => {
                const done = await save.run(
                  () =>
                    api.post(`control/departments/${name}/restore`, {
                      version: picked.version,
                    }),
                  `v${picked.version} is live again, as a new version`,
                );
                if (done) {
                  await onDone();
                  onClose();
                }
              }}
            >
              Make v{picked.version} live
            </button>
          </div>
        </>
      ) : (
        <Lock>
          Pick an earlier version to compare it with the live one. Making it
          live adds a new version; nothing is lost.
        </Lock>
      )}
    </Dialog>
  );
}
