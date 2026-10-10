"use client";

/**
 * Agents (designs: CcAgents): every agent, filterable, with its switch,
 * level, tier, budget and tools. A new agent comes from the intake form and
 * starts switched off; switching it on is its own action.
 */

import { useMemo, useState } from "react";

import {
  Cost,
  Dialog,
  Empty,
  Field,
  Head,
  HistoryButton,
  Lock,
  Problem,
  Tag,
  Toggle,
  useLoad,
  useSave,
} from "@/components/control/kit";
import { api } from "@/lib/api";
import { agentName, departmentName, money } from "@/lib/format";

export interface CcAgent {
  name: string;
  role: string;
  role_type: string;
  runner: string;
  tier: string;
  level: string;
  enabled: boolean;
  daily_budget_usd: number;
  tools: string[];
  department: string | null;
  spend_today_usd: number;
  spend_7d_usd: number;
  prompts: Record<string, number>;
}

interface Tool {
  name: string;
  description: string;
  risk_class: string;
  enabled: boolean;
  source: string;
}

const LEVELS = ["L0", "L1", "L2", "L3"] as const;
const LEVEL_WORDS: Record<string, string> = {
  L0: "asks for everything",
  L1: "reads alone, asks to act",
  L2: "acts on low risk alone",
  L3: "acts alone up to R3",
};

function roleWords(a: { role_type: string }) {
  return a.role_type === "chief_of_staff"
    ? "Chief of Staff"
    : a.role_type === "head"
      ? "Head"
      : "Worker";
}

export function Agents() {
  const list = useLoad<CcAgent[]>("control/agents", /^(agent_|charter_)/);
  const [dept, setDept] = useState("");
  const [state, setState] = useState("");
  const [find, setFind] = useState("");
  const [chosen, setChosen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const save = useSave();
  const agents = list.data ?? [];
  const depts = [...new Set(agents.map((a) => a.department ?? ""))]
    .filter(Boolean)
    .sort();
  const shown = agents.filter(
    (a) =>
      (!dept || a.department === dept) &&
      (!state || (state === "on" ? a.enabled : !a.enabled)) &&
      (!find ||
        `${a.name} ${a.role} ${a.runner}`
          .toLowerCase()
          .includes(find.toLowerCase())),
  );
  const picked = agents.find((a) => a.name === chosen) ?? null;

  async function setOn(a: CcAgent, on: boolean) {
    await save.run(
      () => api.post(`agents/${a.name}/${on ? "enable" : "disable"}`),
      `${agentName(a.name)} switched ${on ? "on" : "off"}`,
    );
    await list.reload();
  }

  async function bulk(on: boolean) {
    await save.run(
      async () => {
        for (const a of shown.filter((x) => x.enabled !== on))
          await api.post(`agents/${a.name}/${on ? "enable" : "disable"}`);
      },
      `${shown.length} agents switched ${on ? "on" : "off"}`,
    );
    await list.reload();
  }

  async function setLevel(a: CcAgent, level: string) {
    await save.run(
      () => api.post(`agents/${a.name}/autonomy`, { level }),
      `${agentName(a.name)} is now ${level}`,
    );
    await list.reload();
  }

  return (
    <>
      <Head
        title="Agents"
        lede="Who works here. New agents start switched off; switching one on is your go."
      >
        <HistoryButton prefix="agent_" title="Agents" />
        <button
          type="button"
          className="btn pri"
          onClick={() => setAdding(true)}
        >
          New agent
        </button>
      </Head>
      <div className="cc-actions" style={{ alignItems: "center" }}>
        <input
          className="inp search"
          placeholder="Search agents"
          aria-label="Search agents"
          value={find}
          onChange={(e) => setFind(e.target.value)}
        />
        <select
          className="inp"
          style={{ width: 170 }}
          aria-label="Department"
          value={dept}
          onChange={(e) => setDept(e.target.value)}
        >
          <option value="">All departments</option>
          {depts.map((d) => (
            <option key={d} value={d}>
              {departmentName(d)}
            </option>
          ))}
        </select>
        <select
          className="inp"
          style={{ width: 120 }}
          aria-label="State"
          value={state}
          onChange={(e) => setState(e.target.value)}
        >
          <option value="">On and off</option>
          <option value="on">On</option>
          <option value="off">Off</option>
        </select>
        <span className="faint" style={{ fontSize: 12, marginLeft: "auto" }}>
          {shown.length} shown
        </span>
        <button
          type="button"
          className="btn sm glass"
          disabled={save.busy || !shown.length}
          onClick={() => void bulk(true)}
        >
          Switch shown on
        </button>
        <button
          type="button"
          className="btn sm glass"
          disabled={save.busy || !shown.length}
          onClick={() => void bulk(false)}
        >
          Switch shown off
        </button>
      </div>
      <Problem text={list.error ?? save.error} />
      <div className="cc-split wide-left">
        <div className="tbl scroll-x">
          <div
            className="th"
            style={{
              gridTemplateColumns:
                "minmax(150px,1.3fr) 90px 80px 90px 80px 90px",
            }}
          >
            <span>Agent</span>
            <span>Runner</span>
            <span>Tier</span>
            <span>Level</span>
            <span>Today</span>
            <span>On</span>
          </div>
          {shown.map((a) => (
            <div
              key={a.name}
              className={`row${a.name === chosen ? " sel" : ""}${a.enabled ? "" : " dim"}`}
              style={{
                gridTemplateColumns:
                  "minmax(150px,1.3fr) 90px 80px 90px 80px 90px",
                paddingTop: 6,
                paddingBottom: 6,
              }}
            >
              <button
                type="button"
                style={{ all: "unset", cursor: "pointer", minWidth: 0 }}
                onClick={() => setChosen(a.name)}
              >
                {agentName(a.name)}
                <span className="sub">
                  {roleWords(a)}, {departmentName(a.department)}
                </span>
              </button>
              <span>{a.runner}</span>
              <span>{a.tier}</span>
              <select
                className="inp"
                style={{ height: 30, padding: "0 8px" }}
                aria-label={`${agentName(a.name)} level`}
                value={a.level}
                onChange={(e) => void setLevel(a, e.target.value)}
              >
                {LEVELS.map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </select>
              <span className="num">{money(a.spend_today_usd)}</span>
              <Toggle
                on={a.enabled}
                label={`${agentName(a.name)} on or off`}
                disabled={save.busy}
                onChange={(on) => void setOn(a, on)}
              />
            </div>
          ))}
          {list.data && !shown.length ? (
            <Empty title="No agents match">
              Clear the filters, or add one with New agent.
            </Empty>
          ) : null}
        </div>
        {picked ? (
          <AgentCard key={picked.name} agent={picked} onChanged={list.reload} />
        ) : (
          <Empty title="Pick an agent">
            Its tier, budget and tools open here. Levels:{" "}
            {LEVELS.map((l) => `${l} ${LEVEL_WORDS[l]}`).join("; ")}.
          </Empty>
        )}
      </div>
      {adding ? (
        <Intake
          depts={depts}
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

function AgentCard({
  agent,
  onChanged,
}: {
  agent: CcAgent;
  onChanged: () => Promise<void>;
}) {
  const tools = useLoad<Tool[]>("control/tools");
  const [tier, setTier] = useState(agent.tier);
  const [budget, setBudget] = useState(
    agent.daily_budget_usd ? String(agent.daily_budget_usd) : "",
  );
  const [picked, setPicked] = useState<Set<string>>(new Set(agent.tools));
  const save = useSave();
  const changed =
    tier !== agent.tier ||
    (budget === "" ? 0 : Number(budget)) !== agent.daily_budget_usd ||
    [...picked].sort().join() !== [...agent.tools].sort().join();

  async function apply() {
    const body: Record<string, unknown> = {};
    if (tier !== agent.tier) body.tier = tier;
    if ((budget === "" ? 0 : Number(budget)) !== agent.daily_budget_usd)
      body.daily_budget_usd = budget === "" ? 0 : Number(budget);
    if ([...picked].sort().join() !== [...agent.tools].sort().join())
      body.tools = [...picked];
    const done = await save.run(
      () => api.post(`control/agents/${agent.name}/settings`, body),
      `${agentName(agent.name)} saved`,
    );
    if (done) await onChanged();
  }

  return (
    <div className="cc-card">
      <div className="sech">
        <h2>{agentName(agent.name)}</h2>
        <Tag kind={agent.enabled ? "live" : "off"}>
          {agent.enabled ? "On" : "Off"}
        </Tag>
      </div>
      <span className="muted" style={{ fontSize: 13 }}>
        {roleWords(agent)} in {departmentName(agent.department)}, runs as{" "}
        {agent.role} on the {agent.runner} runner, level {agent.level} (
        {LEVEL_WORDS[agent.level]}).
      </span>
      <Cost today={agent.spend_today_usd} week={agent.spend_7d_usd} />
      <div className="fields">
        <Field label="Model tier">
          <select
            className="inp"
            value={tier}
            onChange={(e) => setTier(e.target.value)}
          >
            {["cheap", "standard", "frontier"].map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </Field>
        <Field
          label="Own daily cap ($)"
          hint="Empty: the department budget governs"
        >
          <input
            className="inp"
            inputMode="decimal"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
          />
        </Field>
      </div>
      <div className="fld">
        <span className="lbl">Tools it may use</span>
        <div className="chipset">
          {(tools.data ?? []).map((t) => (
            <button
              key={t.name}
              type="button"
              className="chip"
              aria-pressed={picked.has(t.name)}
              title={`${t.risk_class}: ${t.description}`}
              onClick={() => {
                const next = new Set(picked);
                if (next.has(t.name)) next.delete(t.name);
                else next.add(t.name);
                setPicked(next);
              }}
            >
              {picked.has(t.name) ? "✓ " : ""}
              {t.name.replace(/^mcp_/, "").replace(/_/g, " ")}
            </button>
          ))}
        </div>
        <span className="sub">
          A tool the agent may use still follows its risk class and the
          agent&apos;s level.
        </span>
      </div>
      <Lock>
        Changes to a charter agent are made in its charter, so applying the
        charter later keeps them.
      </Lock>
      <Problem text={save.error} />
      <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
        <a
          className="btn glass"
          href="#prompts"
          onClick={() => sessionStorage.setItem("cc:prompts", agent.name)}
        >
          Prompts ({Object.keys(agent.prompts).length})
        </a>
        <button
          type="button"
          className="btn pri"
          disabled={save.busy || !changed}
          onClick={() => void apply()}
        >
          Save
        </button>
      </div>
    </div>
  );
}

function Intake({
  depts,
  onClose,
  onDone,
}: {
  depts: string[];
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const [form, setForm] = useState({
    department: depts[0] ?? "",
    name: "",
    role: "",
    role_type: "worker",
    runner: "deep",
    tier: "cheap",
    daily_budget_usd: "",
    autonomy_level: "L1",
    prompt: "",
  });
  const save = useSave();
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm({ ...form, [k]: e.target.value });
  const slug = useMemo(() => /^[a-z][a-z0-9_-]{1,62}$/, []);

  async function create() {
    if (!slug.test(form.name) || !slug.test(form.role)) {
      save.setError(
        "Name and role: lower case letters, digits, - or _, starting with a letter.",
      );
      return;
    }
    const done = await save.run(
      () =>
        api.post("agents", {
          department: form.department,
          name: form.name,
          role: form.role,
          role_type: form.role_type,
          runner: form.runner,
          tier: form.tier,
          autonomy_level: form.autonomy_level,
          daily_budget_usd: form.daily_budget_usd
            ? Number(form.daily_budget_usd)
            : null,
          prompts: form.prompt.trim() ? { system: form.prompt } : {},
        }),
      `${agentName(form.name)} created, switched off`,
    );
    if (done) await onDone();
  }

  return (
    <Dialog title="New agent" onClose={onClose}>
      <div className="fields">
        <Field label="Department">
          <select
            className="inp"
            value={form.department}
            onChange={set("department")}
          >
            {depts.map((d) => (
              <option key={d} value={d}>
                {departmentName(d)}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Name" hint="e.g. patent-watcher">
          <input className="inp" value={form.name} onChange={set("name")} />
        </Field>
        <Field label="Role" hint="e.g. research">
          <input className="inp" value={form.role} onChange={set("role")} />
        </Field>
        <Field label="Role type">
          <select
            className="inp"
            value={form.role_type}
            onChange={set("role_type")}
          >
            <option value="worker">Worker</option>
            <option value="head">Head</option>
          </select>
        </Field>
        <Field label="Runner">
          <select className="inp" value={form.runner} onChange={set("runner")}>
            {["deep", "pipeline", "router", "digest"].map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </Field>
        <Field label="Model tier">
          <select className="inp" value={form.tier} onChange={set("tier")}>
            {["cheap", "standard", "frontier"].map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </Field>
        <Field label="Autonomy level">
          <select
            className="inp"
            value={form.autonomy_level}
            onChange={set("autonomy_level")}
          >
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l}, {LEVEL_WORDS[l]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Own daily cap ($)" hint="Optional">
          <input
            className="inp"
            inputMode="decimal"
            value={form.daily_budget_usd}
            onChange={set("daily_budget_usd")}
          />
        </Field>
        <Field label="Starting prompt (system)" wide>
          <textarea
            className="inp"
            value={form.prompt}
            onChange={set("prompt")}
          />
        </Field>
      </div>
      <Lock>
        The agent is created <b>switched off</b>. Give it tools on its card,
        then switch it on.
      </Lock>
      <Problem text={save.error} />
      <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
        <button
          type="button"
          className="btn pri"
          disabled={save.busy || !form.name || !form.role}
          onClick={() => void create()}
        >
          Create, switched off
        </button>
      </div>
    </Dialog>
  );
}
