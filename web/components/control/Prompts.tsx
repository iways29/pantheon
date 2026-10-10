"use client";

/**
 * Prompts (designs: CcPrompts). Each agent's prompts sit in named slots
 * (system, explain, extract, brief). Editing publishes a new version, live
 * from the next run; any earlier version can be compared and made live again.
 */

import { useEffect, useState } from "react";

import type { CcAgent } from "@/components/control/Agents";
import {
  Compare,
  Empty,
  Field,
  Head,
  HistoryButton,
  Problem,
  Tag,
  useLoad,
  useSave,
} from "@/components/control/kit";
import { api } from "@/lib/api";
import { agentName, departmentName, when } from "@/lib/format";

interface PromptVersion {
  slot: string;
  version: number;
  body: string;
  note: string | null;
  active: boolean;
  at: string;
  runs: number;
}

const SLOT_WORDS: Record<string, string> = {
  system: "How it works and what it does",
  explain: "How it explains itself on an approval card",
  extract: "How it pulls facts from a page",
  brief: "How it writes the morning brief",
  chat: "How it talks with you in the chat",
};

export function Prompts() {
  const agents = useLoad<CcAgent[]>("control/agents", /^agent_/);
  const [agent, setAgent] = useState<string | null>(null);
  useEffect(() => {
    const asked =
      typeof window !== "undefined"
        ? sessionStorage.getItem("cc:prompts")
        : null;
    if (asked) {
      sessionStorage.removeItem("cc:prompts");
      setAgent(asked);
    } else if (!agent && agents.data?.length)
      setAgent(
        agents.data.find((a) => a.role_type === "chief_of_staff")?.name ??
          agents.data[0]!.name,
      );
  }, [agents.data, agent]);
  const total = (agents.data ?? []).reduce(
    (n, a) => n + Object.keys(a.prompts).length,
    0,
  );

  return (
    <>
      <Head
        title="Prompts"
        lede={`What each agent is told. ${total} live prompts; each edit is a new version, live from the next run.`}
      >
        <HistoryButton prefix="agent_prompt_" title="Prompts" />
        <select
          className="inp"
          style={{ width: 230 }}
          aria-label="Agent"
          value={agent ?? ""}
          onChange={(e) => setAgent(e.target.value)}
        >
          {(agents.data ?? []).map((a) => (
            <option key={a.name} value={a.name}>
              {agentName(a.name)} · {departmentName(a.department)}
            </option>
          ))}
        </select>
      </Head>
      <Problem text={agents.error} />
      {agent ? <AgentPrompts key={agent} agent={agent} /> : null}
    </>
  );
}

function AgentPrompts({ agent }: { agent: string }) {
  const versions = useLoad<PromptVersion[]>(
    `control/agents/${agent}/prompts`,
    /^agent_prompt_/,
  );
  const all = versions.data ?? [];
  const slots = [...new Set(all.map((v) => v.slot))];
  const [slot, setSlot] = useState<string | null>(null);
  useEffect(() => {
    if (!slot && slots.length)
      setSlot(slots.includes("system") ? "system" : slots[0]!);
  }, [slots, slot]);
  if (versions.data && !all.length)
    return (
      <Empty title="No prompts yet">
        This agent has no prompts stored. Its runner uses its starting prompt on
        first use.
      </Empty>
    );
  if (!slot)
    return <span className="faint">{versions.error ?? "Loading"}</span>;
  const ofSlot = all.filter((v) => v.slot === slot);
  return (
    <>
      <div
        className="seg"
        role="group"
        aria-label="Prompt slots"
        style={{ alignSelf: "flex-start" }}
      >
        {slots.map((s) => (
          <button
            key={s}
            type="button"
            aria-pressed={s === slot}
            onClick={() => setSlot(s)}
          >
            {s}
          </button>
        ))}
      </div>
      <SlotEditor
        key={`${agent}:${slot}`}
        agent={agent}
        slot={slot}
        versions={ofSlot}
        onChanged={versions.reload}
      />
    </>
  );
}

function SlotEditor({
  agent,
  slot,
  versions,
  onChanged,
}: {
  agent: string;
  slot: string;
  versions: PromptVersion[];
  onChanged: () => Promise<void>;
}) {
  const live = versions.find((v) => v.active) ?? versions[0]!;
  const [text, setText] = useState(live.body);
  const [note, setNote] = useState("");
  const [compare, setCompare] = useState<number | null>(null);
  const save = useSave();
  useEffect(() => setText(live.body), [live.version, live.body]);
  const other = versions.find((v) => v.version === compare);

  async function publish() {
    const done = await save.run(
      () =>
        api.post<{ version: number }>(
          `control/agents/${agent}/prompts/${slot}`,
          { body: text, note: note || null },
        ),
      `${agentName(agent)} ${slot} prompt saved as a new version`,
    );
    if (done) {
      setNote("");
      await onChanged();
    }
  }

  async function makeLive(version: number) {
    const done = await save.run(
      () =>
        api.post(`control/agents/${agent}/prompts/${slot}/activate`, {
          version,
        }),
      `v${version} is live again`,
    );
    if (done) {
      setCompare(null);
      await onChanged();
    }
  }

  return (
    <div className="cc-split wide-left">
      <div className="cc-card">
        <div className="sech">
          <h2>
            {agentName(agent)}, {slot}
          </h2>
          <Tag kind="live">Live v{live.version}</Tag>
        </div>
        <span className="muted" style={{ fontSize: 13 }}>
          {SLOT_WORDS[slot] ?? "A prompt slot"}. Used by {live.runs}{" "}
          {live.runs === 1 ? "run" : "runs"}.
        </span>
        <textarea
          className="inp"
          style={{ minHeight: 340 }}
          aria-label={`${slot} prompt`}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <Field label="Note (why this change)">
          <input
            className="inp"
            value={note}
            maxLength={300}
            onChange={(e) => setNote(e.target.value)}
          />
        </Field>
        <Problem text={save.error} />
        <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
          <button
            type="button"
            className="btn glass"
            disabled={text === live.body}
            onClick={() => setText(live.body)}
          >
            Discard
          </button>
          <button
            type="button"
            className="btn pri"
            disabled={save.busy || !text.trim() || text === live.body}
            onClick={() => void publish()}
          >
            Save as v{Math.max(...versions.map((v) => v.version)) + 1}
          </button>
        </div>
        {other ? (
          <>
            <Compare
              left={other.body}
              right={live.body}
              leftLabel={`v${other.version}`}
              rightLabel={`v${live.version}, live`}
            />
            <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
              <button
                type="button"
                className="btn glass"
                onClick={() => setCompare(null)}
              >
                Close comparison
              </button>
              <button
                type="button"
                className="btn pri"
                disabled={save.busy}
                onClick={() => void makeLive(other.version)}
              >
                Make v{other.version} live
              </button>
            </div>
          </>
        ) : null}
      </div>
      <div className="cc-card">
        <h3>Versions</h3>
        <div className="tbl">
          {versions.map((v) => (
            <div
              key={v.version}
              className={`row${v.active ? " sel" : ""}`}
              style={{
                gridTemplateColumns: "44px minmax(0,1fr) auto",
                paddingTop: 8,
                paddingBottom: 8,
              }}
            >
              <span className="num">v{v.version}</span>
              <span>
                <span className="clamp2">{v.note ?? "No note"}</span>
                <span className="sub">
                  {when(v.at)} · {v.runs} {v.runs === 1 ? "run" : "runs"}
                </span>
              </span>
              {v.active ? (
                <Tag kind="live">Live</Tag>
              ) : (
                <button
                  type="button"
                  className="btn sm glass"
                  onClick={() => setCompare(v.version)}
                >
                  Compare
                </button>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
