"use client";

/**
 * Tools and MCP (designs: CcTools, CcToolsMcp, CcToolsAssign). Built-in
 * tools with their risk and limits; MCP servers and the tools they offer,
 * each off until approved; and who may use what, with what would happen.
 */

import { useState } from "react";

import type { CcAgent } from "@/components/control/Agents";
import {
  Empty,
  Field,
  Head,
  HistoryButton,
  Lock,
  modeWords,
  Problem,
  Tag,
  Toggle,
  useLoad,
  useSave,
} from "@/components/control/kit";
import { api } from "@/lib/api";
import { agentName } from "@/lib/format";

interface Tool {
  name: string;
  description: string;
  risk_class: string;
  approval: string;
  enabled: boolean;
  timeout_seconds: number;
  max_output_chars: number;
  max_calls_per_day: number | null;
  settings: Record<string, unknown>;
  source: string;
  suggested_risk: string | null;
  server: string | null;
  changed: boolean;
  calls_today: number;
  agents: string[];
}

interface Server {
  name: string;
  url: string;
  auth: string;
  status: string;
  last_error: string | null;
  tools: number;
  tools_on: number;
  media_hosts: string[];
}

interface McpTool {
  name: string;
  server: string;
  description: string;
  input_schema: Record<string, unknown> | null;
  annotations: Record<string, unknown> | null;
  suggested_risk: string | null;
  risk_class: string;
  enabled: boolean;
  max_calls_per_day: number | null;
  changed: boolean;
}

export const RISK_WORDS: Record<string, string> = {
  R0: "Reads nothing outside; no effect",
  R1: "Reads the brain or the web",
  R2: "Writes inside Pantheon",
  R3: "Acts outside, reversibly",
  R4: "Spends money or acts for good: always asks",
};

const TABS = [
  ["builtin", "Built-in"],
  ["mcp", "MCP servers"],
  ["assign", "Who uses what"],
] as const;

export function Tools() {
  const [tab, setTab] = useState<(typeof TABS)[number][0]>("builtin");
  const tools = useLoad<Tool[]>("control/tools", /^(tool_|mcp_)/);
  return (
    <>
      <Head
        title="Tools and MCP"
        lede="What agents can do, how risky each tool is, and who may use it."
      >
        <HistoryButton prefix="tool_,mcp_" title="Tools" />
      </Head>
      <div
        className="seg"
        role="group"
        aria-label="Tools views"
        style={{ alignSelf: "flex-start" }}
      >
        {TABS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            aria-pressed={tab === key}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>
      <Problem text={tools.error} />
      {tab === "builtin" ? (
        <Builtin
          tools={(tools.data ?? []).filter((t) => t.source === "builtin")}
          onChanged={tools.reload}
        />
      ) : null}
      {tab === "mcp" ? <Mcp onChanged={tools.reload} /> : null}
      {tab === "assign" ? <Assign tools={tools.data ?? []} /> : null}
    </>
  );
}

function Builtin({
  tools,
  onChanged,
}: {
  tools: Tool[];
  onChanged: () => Promise<void>;
}) {
  const [chosen, setChosen] = useState<string | null>(null);
  const picked = tools.find((t) => t.name === chosen) ?? null;
  return (
    <div className="cc-split wide-left">
      <div className="tbl scroll-x">
        <div
          className="th"
          style={{
            gridTemplateColumns: "minmax(170px,1fr) 60px 90px 70px 80px",
          }}
        >
          <span>Tool</span>
          <span>Risk</span>
          <span>Approval</span>
          <span>Today</span>
          <span>On</span>
        </div>
        {tools.map((t) => (
          <button
            key={t.name}
            type="button"
            className={`row${t.name === chosen ? " sel" : ""}${t.enabled ? "" : " dim"}`}
            style={{
              gridTemplateColumns: "minmax(170px,1fr) 60px 90px 70px 80px",
            }}
            onClick={() => setChosen(t.name)}
          >
            <span>
              {t.name.replace(/_/g, " ")}
              <span className="sub clamp2">{t.description}</span>
            </span>
            <Tag kind="r">{t.risk_class}</Tag>
            <span>{t.approval === "auto" ? "By level" : "Asks you"}</span>
            <span className="num">{t.calls_today}</span>
            <span>{t.enabled ? "On" : "Off"}</span>
          </button>
        ))}
      </div>
      {picked ? (
        <ToolCard key={picked.name} tool={picked} onChanged={onChanged} />
      ) : (
        <Empty title="Pick a tool">
          Its risk class, approval, limits and settings open here.
        </Empty>
      )}
    </div>
  );
}

function ToolCard({
  tool,
  onChanged,
}: {
  tool: Tool;
  onChanged: () => Promise<void>;
}) {
  const [form, setForm] = useState({
    risk_class: tool.risk_class,
    approval: tool.approval,
    enabled: tool.enabled,
    timeout_seconds: String(tool.timeout_seconds),
    max_output_chars: String(tool.max_output_chars),
    max_calls_per_day: tool.max_calls_per_day
      ? String(tool.max_calls_per_day)
      : "",
    settings: Object.keys(tool.settings).length
      ? JSON.stringify(tool.settings, null, 2)
      : "",
  });
  const save = useSave();
  const r4 = form.risk_class === "R4";

  async function apply() {
    let settings: Record<string, unknown> | undefined;
    if (form.settings.trim()) {
      try {
        settings = JSON.parse(form.settings) as Record<string, unknown>;
      } catch {
        save.setError("Settings are not valid JSON.");
        return;
      }
    }
    const done = await save.run(
      () =>
        api.put(`control/tools/${tool.name}`, {
          risk_class: form.risk_class,
          approval: r4 ? "approval" : form.approval,
          enabled: tool.source === "mcp" ? undefined : form.enabled,
          timeout_seconds: Number(form.timeout_seconds),
          max_output_chars: Number(form.max_output_chars),
          max_calls_per_day: form.max_calls_per_day
            ? Number(form.max_calls_per_day)
            : undefined,
          settings,
        }),
      `${tool.name.replace(/_/g, " ")} saved`,
    );
    if (done) await onChanged();
  }

  return (
    <div className="cc-card">
      <div className="sech">
        <h2>{tool.name.replace(/_/g, " ")}</h2>
        <Toggle
          on={form.enabled}
          label="On or off"
          onChange={(enabled) => setForm({ ...form, enabled })}
        />
      </div>
      <span className="muted" style={{ fontSize: 13, lineHeight: 1.5 }}>
        {tool.description}
      </span>
      <div className="fields">
        <Field label="Risk class" hint={RISK_WORDS[form.risk_class]}>
          <select
            className="inp"
            value={form.risk_class}
            onChange={(e) => setForm({ ...form, risk_class: e.target.value })}
          >
            {["R0", "R1", "R2", "R3", "R4"].map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </Field>
        <Field
          label="Approval"
          hint={
            r4
              ? "R4 always asks you"
              : form.approval === "auto"
                ? "Follows the agent's level"
                : "Asks you every time"
          }
        >
          <select
            className="inp"
            value={r4 ? "approval" : form.approval}
            disabled={r4}
            onChange={(e) => setForm({ ...form, approval: e.target.value })}
          >
            <option value="auto">By the agent&apos;s level</option>
            <option value="approval">Always ask me</option>
          </select>
        </Field>
        <Field label="Timeout (seconds)">
          <input
            className="inp"
            inputMode="numeric"
            value={form.timeout_seconds}
            onChange={(e) =>
              setForm({ ...form, timeout_seconds: e.target.value })
            }
          />
        </Field>
        <Field label="Output cap (characters)">
          <input
            className="inp"
            inputMode="numeric"
            value={form.max_output_chars}
            onChange={(e) =>
              setForm({ ...form, max_output_chars: e.target.value })
            }
          />
        </Field>
        <Field label="Calls a day" hint="Empty: no cap">
          <input
            className="inp"
            inputMode="numeric"
            value={form.max_calls_per_day}
            onChange={(e) =>
              setForm({ ...form, max_calls_per_day: e.target.value })
            }
          />
        </Field>
      </div>
      <Field
        label="Settings (JSON)"
        hint="For web search: the engine, results per search, searches a day"
      >
        <textarea
          className="inp code"
          style={{ minHeight: 80 }}
          value={form.settings}
          onChange={(e) => setForm({ ...form, settings: e.target.value })}
        />
      </Field>
      <span className="sub">
        Used by:{" "}
        {tool.agents.length
          ? tool.agents.map(agentName).join(", ")
          : "nobody yet"}
      </span>
      {r4 ? (
        <Lock>
          <b>R4 always asks you.</b> It spends money or acts for good, so no
          level runs it alone.
        </Lock>
      ) : null}
      <Problem text={save.error} />
      <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
        <button
          type="button"
          className="btn pri"
          disabled={save.busy}
          onClick={() => void apply()}
        >
          Save
        </button>
      </div>
    </div>
  );
}

/** One server: sign in, list its tools again, and the hosts whose links
 * reach you even when its words are withheld (ADR 040). Its own problems
 * show on its own row. */
function ServerRow({
  server: s,
  onChanged,
}: {
  server: Server;
  onChanged: () => Promise<void>;
}) {
  const save = useSave();
  const [hosts, setHosts] = useState((s.media_hosts ?? []).join(", "));
  const connected = s.status === "connected";
  const saved = (s.media_hosts ?? []).join(", ");

  async function connect() {
    const got = await save.run(() =>
      api.post<{ authorize_url?: string }>(`mcp/servers/${s.name}/connect`),
    );
    if (got?.authorize_url)
      window.open(got.authorize_url, "_blank", "noopener");
    await onChanged();
  }

  async function refresh() {
    await save.run(
      () => api.post(`mcp/servers/${s.name}/refresh`),
      `${s.name}: tools listed again`,
    );
    await onChanged();
  }

  async function saveHosts() {
    const done = await save.run(
      () =>
        api.put(`mcp/servers/${s.name}/media-hosts`, {
          hosts: hosts.split(/[\s,]+/).filter(Boolean),
        }),
      `${s.name}: trusted hosts saved`,
    );
    if (done) await onChanged();
  }

  return (
    <div
      className="row"
      style={{
        gridTemplateColumns: "minmax(0,1fr) auto",
        paddingTop: 8,
        paddingBottom: 8,
      }}
    >
      <span>
        {s.name}{" "}
        <Tag kind={connected ? "ok" : s.status === "error" ? "bad" : "flag"}>
          {s.status.replace("_", " ")}
        </Tag>
        <span className="sub">
          {s.url} · {s.tools_on} of {s.tools} tools on
        </span>
        {s.last_error ? <span className="err">{s.last_error}</span> : null}
      </span>
      <span className="cc-actions">
        <button
          type="button"
          className="btn sm glass"
          disabled={save.busy}
          title={
            connected
              ? "Already connected; only needed if it stops working"
              : undefined
          }
          onClick={() => void connect()}
        >
          {s.auth === "oauth"
            ? connected
              ? "Sign in again"
              : "Sign in"
            : connected
              ? "Reconnect"
              : "Connect"}
        </button>
        <button
          type="button"
          className="btn sm glass"
          disabled={save.busy || !connected}
          onClick={() => void refresh()}
        >
          Refresh tools
        </button>
      </span>
      <div
        style={{
          gridColumn: "1 / -1",
          display: "flex",
          gap: 8,
          alignItems: "end",
          flexWrap: "wrap",
        }}
      >
        <Field
          label="Trusted image hosts"
          hint="Links on these hosts reach you even when the tool's words are withheld"
        >
          <input
            className="inp code"
            style={{ minWidth: 260 }}
            placeholder="e.g. cdn.example.com"
            value={hosts}
            onChange={(e) => setHosts(e.target.value)}
          />
        </Field>
        <button
          type="button"
          className="btn sm glass"
          disabled={save.busy || hosts.trim() === saved}
          onClick={() => void saveHosts()}
        >
          Save hosts
        </button>
      </div>
      <div style={{ gridColumn: "1 / -1" }}>
        <Problem text={save.error} />
      </div>
    </div>
  );
}

function Mcp({ onChanged }: { onChanged: () => Promise<void> }) {
  const servers = useLoad<Server[]>("mcp/servers", /^mcp_/);
  const tools = useLoad<McpTool[]>("mcp/tools", /^(mcp_|tool_)/);
  const [form, setForm] = useState({
    name: "",
    url: "",
    auth: "oauth",
    token: "",
  });
  const save = useSave();
  const [open, setOpen] = useState<string | null>(null);

  async function reloadAll() {
    await Promise.all([servers.reload(), tools.reload(), onChanged()]);
  }

  async function add() {
    const done = await save.run(
      () =>
        api.post("mcp/servers", {
          name: form.name,
          url: form.url,
          auth: form.auth,
          token: form.auth === "bearer" ? form.token : null,
        }),
      `${form.name} added; sign in to list its tools`,
    );
    if (done) {
      setForm({ name: "", url: "", auth: "oauth", token: "" });
      await reloadAll();
    }
  }

  async function approve(t: McpTool, risk: string, cap: string) {
    await save.run(
      () =>
        api.post(`mcp/tools/${t.name}/approve`, {
          risk_class: risk,
          max_calls_per_day: cap ? Number(cap) : null,
        }),
      `${t.name} approved and switched on`,
    );
    await reloadAll();
  }

  async function off(t: McpTool) {
    await save.run(
      () => api.post(`mcp/tools/${t.name}/off`),
      `${t.name} switched off`,
    );
    await reloadAll();
  }

  const changed = (tools.data ?? []).filter((t) => t.changed);
  return (
    <div className="cc-split">
      <div style={{ display: "grid", gap: 14 }}>
        <div className="cc-card">
          <h2>Servers</h2>
          {(servers.data ?? []).map((s) => (
            <ServerRow key={s.name} server={s} onChanged={reloadAll} />
          ))}
          {servers.data && !servers.data.length ? (
            <Empty title="No MCP servers yet">
              Add one by its address below, then sign in.
            </Empty>
          ) : null}
          <div
            className="fields"
            style={{ borderTop: "1px solid var(--line)", paddingTop: 12 }}
          >
            <Field label="Name">
              <input
                className="inp"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </Field>
            <Field label="Address (https)">
              <input
                className="inp"
                value={form.url}
                onChange={(e) => setForm({ ...form, url: e.target.value })}
              />
            </Field>
            <Field label="Sign-in">
              <select
                className="inp"
                value={form.auth}
                onChange={(e) => setForm({ ...form, auth: e.target.value })}
              >
                <option value="oauth">Sign in with the service</option>
                <option value="bearer">A token</option>
                <option value="none">None</option>
              </select>
            </Field>
            {form.auth === "bearer" ? (
              <Field
                label="Token"
                hint="Kept in Supabase Vault, never shown again"
              >
                <input
                  className="inp"
                  type="password"
                  value={form.token}
                  onChange={(e) => setForm({ ...form, token: e.target.value })}
                />
              </Field>
            ) : null}
          </div>
          <Problem text={save.error ?? servers.error} />
          <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
            <button
              type="button"
              className="btn pri"
              disabled={
                save.busy || !form.name || !form.url.startsWith("https://")
              }
              onClick={() => void add()}
            >
              Add server
            </button>
          </div>
        </div>
        {changed.length ? (
          <Lock>
            <b>{changed.length} changed by their server, switched off</b> until
            you read and approve them again.
          </Lock>
        ) : null}
      </div>
      <div className="cc-card">
        <h2>Tools found</h2>
        {(tools.data ?? []).map((t) => (
          <McpToolRow
            key={t.name}
            tool={t}
            open={open === t.name}
            onOpen={() => setOpen(open === t.name ? null : t.name)}
            onApprove={approve}
            onOff={off}
            busy={save.busy}
          />
        ))}
        {tools.data && !tools.data.length ? (
          <span className="faint">No tools listed yet.</span>
        ) : null}
      </div>
    </div>
  );
}

function McpToolRow({
  tool,
  open,
  onOpen,
  onApprove,
  onOff,
  busy,
}: {
  tool: McpTool;
  open: boolean;
  onOpen: () => void;
  onApprove: (t: McpTool, risk: string, cap: string) => Promise<void>;
  onOff: (t: McpTool) => Promise<void>;
  busy: boolean;
}) {
  const [risk, setRisk] = useState(
    tool.risk_class || tool.suggested_risk || "R4",
  );
  const [cap, setCap] = useState(
    tool.max_calls_per_day ? String(tool.max_calls_per_day) : "",
  );
  return (
    <div
      className="fld"
      style={{ borderTop: "1px solid var(--line)", paddingTop: 10 }}
    >
      <button
        type="button"
        style={{
          all: "unset",
          cursor: "pointer",
          display: "flex",
          gap: 8,
          alignItems: "center",
          flexWrap: "wrap",
        }}
        aria-expanded={open}
        onClick={onOpen}
      >
        <span>
          {tool.name.replace(/^mcp_[a-z0-9]+_/, "").replace(/_/g, " ")}
        </span>
        <Tag kind="r">{tool.risk_class}</Tag>
        {tool.changed ? (
          <Tag kind="bad">changed, off</Tag>
        ) : (
          <Tag kind={tool.enabled ? "live" : "off"}>
            {tool.enabled ? "On" : "Off"}
          </Tag>
        )}
        <span className="sub" style={{ margin: 0 }}>
          {tool.server}
        </span>
      </button>
      {open ? (
        <>
          <span style={{ fontSize: 13, lineHeight: 1.5 }}>
            {tool.description}
          </span>
          {tool.annotations && Object.keys(tool.annotations).length ? (
            <span className="sub">
              Server&apos;s hints:{" "}
              {Object.entries(tool.annotations)
                .map(([k, v]) => `${k} ${String(v)}`)
                .join(", ")}
            </span>
          ) : null}
          {tool.input_schema ? (
            <pre
              className="code"
              style={{
                whiteSpace: "pre-wrap",
                margin: 0,
                maxHeight: 180,
                overflow: "auto",
              }}
            >
              {JSON.stringify(tool.input_schema, null, 2)}
            </pre>
          ) : null}
          <div className="fields">
            <Field
              label="Risk class"
              hint={`Suggested ${tool.suggested_risk ?? "R4"}. ${RISK_WORDS[risk]}`}
            >
              <select
                className="inp"
                value={risk}
                onChange={(e) => setRisk(e.target.value)}
              >
                {["R0", "R1", "R2", "R3", "R4"].map((r) => (
                  <option key={r}>{r}</option>
                ))}
              </select>
            </Field>
            <Field label="Calls a day" hint="Optional cap">
              <input
                className="inp"
                inputMode="numeric"
                value={cap}
                onChange={(e) => setCap(e.target.value)}
              />
            </Field>
          </div>
          <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
            {tool.enabled ? (
              <button
                type="button"
                className="btn sm glass"
                disabled={busy}
                onClick={() => void onOff(tool)}
              >
                Switch off
              </button>
            ) : null}
            <button
              type="button"
              className="btn sm pri"
              disabled={busy}
              onClick={() => void onApprove(tool, risk, cap)}
            >
              {tool.enabled ? "Save" : "Approve and switch on"}
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}

/** Agents by tools: whether each would run, get a check by Jev, or ask you. */
function Assign({ tools }: { tools: Tool[] }) {
  const agents = useLoad<CcAgent[]>("control/agents", /^agent_|^charter_/);
  const ladder = useLoad<{
    rules: { level: string; risk_class: string; mode: string }[];
  }>("control/autonomy", /^autonomy_/);
  const save = useSave();
  const [all, setAll] = useState(false);
  const shown = tools
    .filter((t) => t.enabled && (all || t.agents.length))
    .slice(0, 40);
  const list = agents.data ?? [];

  function mode(a: CcAgent, t: Tool): string {
    if (!a.tools.includes(t.name)) return "none";
    if (t.risk_class === "R4" || t.approval === "approval") return "hold";
    return (
      ladder.data?.rules.find(
        (r) => r.level === a.level && r.risk_class === t.risk_class,
      )?.mode ?? "hold"
    );
  }

  async function flip(a: CcAgent, t: Tool) {
    const next = a.tools.includes(t.name)
      ? a.tools.filter((x) => x !== t.name)
      : [...a.tools, t.name];
    await save.run(
      () => api.post(`control/agents/${a.name}/settings`, { tools: next }),
      `${agentName(a.name)}: ${t.name.replace(/_/g, " ")} ${a.tools.includes(t.name) ? "removed" : "given"}`,
    );
    await agents.reload();
  }

  return (
    <div className="cc-card desk-only">
      <div className="sech">
        <h2>Who uses what</h2>
        <label
          className="sub"
          style={{ display: "flex", gap: 6, alignItems: "center", margin: 0 }}
        >
          <input
            type="checkbox"
            checked={all}
            onChange={(e) => setAll(e.target.checked)}
          />{" "}
          Show tools nobody uses
        </label>
      </div>
      <span className="sub">
        Each cell: runs alone, Jev checks, asks you, or not given. Tap a cell to
        give or take the tool.
      </span>
      <Problem text={save.error ?? agents.error} />
      <div className="scroll-x">
        <div
          className="grid"
          style={{
            gridTemplateColumns: `150px repeat(${shown.length}, 110px)`,
          }}
        >
          <span />
          {shown.map((t) => (
            <span
              key={t.name}
              className="faint"
              title={t.description}
              style={{ fontSize: 11, lineHeight: 1.2 }}
            >
              {t.name.replace(/^mcp_[a-z0-9]+_/, "").replace(/_/g, " ")}{" "}
              <b>{t.risk_class}</b>
            </span>
          ))}
          {list.map((a) => (
            <AssignRow
              key={a.name}
              agent={a}
              tools={shown}
              mode={mode}
              onFlip={flip}
              busy={save.busy}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function AssignRow({
  agent,
  tools,
  mode,
  onFlip,
  busy,
}: {
  agent: CcAgent;
  tools: Tool[];
  mode: (a: CcAgent, t: Tool) => string;
  onFlip: (a: CcAgent, t: Tool) => Promise<void>;
  busy: boolean;
}) {
  return (
    <>
      <span>
        {agentName(agent.name)} <span className="faint">{agent.level}</span>
      </span>
      {tools.map((t) => {
        const m = mode(agent, t);
        return (
          <button
            key={t.name}
            type="button"
            className={`cell ${m}`}
            disabled={busy}
            aria-label={`${agentName(agent.name)}, ${t.name}: ${m === "none" ? "not given" : modeWords(m)}`}
            onClick={() => void onFlip(agent, t)}
          >
            <span className="m" aria-hidden="true" />
            {m === "none" ? "–" : modeWords(m)}
          </button>
        );
      })}
    </>
  );
}
