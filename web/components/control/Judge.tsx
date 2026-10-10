"use client";

/**
 * Judge (designs: CcJudge): Jev's checks. Each gate's switch, what happens
 * when Jev cannot be reached, its policy and questions, every version, and
 * how often Jev agrees with your decisions.
 */

import { useEffect, useState } from "react";

import {
  Compare,
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
import { when } from "@/lib/format";

interface Gate {
  gate: string;
  version: number;
  enabled: boolean;
  model: string;
  fail_mode: string;
  allow_sensitive: boolean;
  policy: Record<string, unknown>;
  note: string | null;
  at: string;
  versions: number;
  week: number;
  questions: {
    key: string;
    version: number;
    type: string;
    instructions: unknown;
    criteria: unknown;
  }[];
}

interface JudgeData {
  gates: Gate[];
  agreement: {
    action_key: string;
    decided: number;
    recommended: number;
    agreed: number;
    agreement: number | null;
  }[];
}

interface GateVersion {
  version: number;
  active: boolean;
  enabled: boolean;
  fail_mode: string;
  policy: Record<string, unknown>;
  note: string | null;
  at: string;
}

const GATE_WORDS: Record<string, string> = {
  content_screen: "Screens pages and documents before agents read them",
  brain_claim: "Checks a fact before it enters the brain",
  tool_risk: "Weighs a tool call before it runs",
  order_route: "Routes your orders to a department",
  brief_rank: "Ranks what leads the morning brief",
  result_check: "Checks a worker result before it is used",
  recall_rank: "Ranks what the brain recalls",
  memory_triage: "Sorts what you say into memory",
  entity_match: "Matches a mention to a known thing",
  link_support: "Checks whether a fact supports a link",
};

const text = (v: unknown) =>
  typeof v === "string" ? v : JSON.stringify(v, null, 2);

export function Judge() {
  const data = useLoad<JudgeData>("control/judge", /^judge_/);
  const [chosen, setChosen] = useState<string | null>(null);
  const gates = data.data?.gates ?? [];
  useEffect(() => {
    if (!chosen && gates.length) setChosen(gates[0]!.gate);
  }, [chosen, gates]);
  const picked = gates.find((g) => g.gate === chosen) ?? null;
  return (
    <>
      <Head
        title="Judge"
        lede="Jev's checks: typed, probabilistic decisions, each logged. Its confidence is never permission to act."
      >
        <HistoryButton prefix="judge_" title="Judge" />
      </Head>
      <Problem text={data.error} />
      <div className="cc-split">
        <div style={{ display: "grid", gap: 14 }}>
          <div className="tbl">
            <div
              className="th"
              style={{
                gridTemplateColumns: "minmax(150px,1fr) 60px 70px 60px",
              }}
            >
              <span>Check</span>
              <span>Version</span>
              <span>7 days</span>
              <span>On</span>
            </div>
            {gates.map((g) => (
              <button
                key={g.gate}
                type="button"
                className={`row${g.gate === chosen ? " sel" : ""}${g.enabled ? "" : " dim"}`}
                style={{
                  gridTemplateColumns: "minmax(150px,1fr) 60px 70px 60px",
                }}
                onClick={() => setChosen(g.gate)}
              >
                <span>
                  {g.gate.replace(/_/g, " ")}
                  <span className="sub">{GATE_WORDS[g.gate] ?? ""}</span>
                </span>
                <span className="num">v{g.version}</span>
                <span className="num">{g.week}</span>
                <span>{g.enabled ? "On" : "Off"}</span>
              </button>
            ))}
          </div>
          <Agreement rows={data.data?.agreement ?? []} />
        </div>
        {picked ? (
          <GateCard
            key={`${picked.gate}:${picked.version}`}
            gate={picked}
            onChanged={data.reload}
          />
        ) : null}
      </div>
    </>
  );
}

function GateCard({
  gate,
  onChanged,
}: {
  gate: Gate;
  onChanged: () => Promise<void>;
}) {
  const versions = useLoad<GateVersion[]>(
    `control/judge/${gate.gate}/versions`,
  );
  const [policy, setPolicy] = useState(JSON.stringify(gate.policy, null, 2));
  const [note, setNote] = useState("");
  const [compare, setCompare] = useState<number | null>(null);
  const save = useSave();
  const other = versions.data?.find((v) => v.version === compare);

  async function change(body: Record<string, unknown>, done: string) {
    const ok = await save.run(
      () =>
        api.post(`control/judge/${gate.gate}`, { ...body, note: note || null }),
      done,
    );
    if (ok) {
      setNote("");
      await onChanged();
    }
  }

  async function savePolicy() {
    try {
      await change(
        { policy: JSON.parse(policy) as unknown },
        "Policy saved as a new version",
      );
    } catch {
      save.setError("The policy is not valid JSON.");
    }
  }

  return (
    <div className="cc-card">
      <div className="sech">
        <h2>{gate.gate.replace(/_/g, " ")}</h2>
        <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Tag kind="live">Live v{gate.version}</Tag>
          <Toggle
            on={gate.enabled}
            label="Check on or off"
            disabled={save.busy}
            onChange={(on) =>
              void change(
                { enabled: on },
                `${gate.gate} switched ${on ? "on" : "off"}`,
              )
            }
          />
        </span>
      </div>
      <span className="muted" style={{ fontSize: 13 }}>
        {GATE_WORDS[gate.gate] ?? "A Jev check"}. Model {gate.model}.{" "}
        {gate.week} judgments in the last 7 days.
      </span>
      <div className="fields">
        <Field
          label="If Jev cannot be reached"
          hint={
            gate.fail_mode === "closed"
              ? "Closed: the action waits"
              : "Open: the action goes ahead"
          }
        >
          <select
            className="inp"
            value={gate.fail_mode}
            disabled={save.busy}
            onChange={(e) =>
              void change(
                { fail_mode: e.target.value },
                `Fails ${e.target.value} now`,
              )
            }
          >
            <option value="closed">Fail closed (safer)</option>
            <option value="open">Fail open</option>
          </select>
        </Field>
        <Field label="Sensitive data">
          <span style={{ fontSize: 13 }}>
            {gate.allow_sensitive ? "Allowed" : "Not sent (default)"}
          </span>
        </Field>
      </div>
      <Field
        label="Policy: outcomes and thresholds"
        hint="Outcomes least severe first; rules turn Jev's probabilities into an outcome"
      >
        <textarea
          className="inp code"
          style={{ minHeight: 160 }}
          value={policy}
          onChange={(e) => setPolicy(e.target.value)}
        />
      </Field>
      <Field label="Note (why)">
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
          className="btn pri"
          disabled={
            save.busy || policy === JSON.stringify(gate.policy, null, 2)
          }
          onClick={() => void savePolicy()}
        >
          Save policy as v{gate.versions + 1}
        </button>
      </div>
      <h3>Questions</h3>
      {gate.questions.length ? (
        gate.questions.map((q) => (
          <div
            key={q.key}
            className="fld"
            style={{ borderTop: "1px solid var(--line)", paddingTop: 8 }}
          >
            <span>
              <b>{q.key}</b>{" "}
              <span className="faint">
                v{q.version},{" "}
                {q.type === "noul"
                  ? "yes or no"
                  : q.type === "choice"
                    ? "choose one"
                    : q.type}
              </span>
            </span>
            <span
              style={{ fontSize: 13, lineHeight: 1.5, whiteSpace: "pre-wrap" }}
            >
              {text(q.instructions)}
            </span>
          </div>
        ))
      ) : (
        <span className="faint">No questions.</span>
      )}
      <Lock>
        Question wording changes with scripts.judge for now; every version is
        kept and logged.
      </Lock>
      <h3>Versions</h3>
      <div className="tbl">
        {(versions.data ?? []).map((v) => (
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
                {when(v.at)} · {v.enabled ? "on" : "off"}, fails {v.fail_mode}
              </span>
            </span>
            {v.active ? (
              <Tag kind="live">Live</Tag>
            ) : (
              <button
                type="button"
                className="btn sm glass"
                onClick={() =>
                  setCompare(compare === v.version ? null : v.version)
                }
              >
                Compare
              </button>
            )}
          </div>
        ))}
      </div>
      {other ? (
        <>
          <Compare
            left={JSON.stringify(other.policy, null, 2)}
            right={JSON.stringify(gate.policy, null, 2)}
            leftLabel={`v${other.version}`}
            rightLabel={`v${gate.version}, live`}
          />
          <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
            <button
              type="button"
              className="btn pri"
              disabled={save.busy}
              onClick={async () => {
                const ok = await save.run(
                  () =>
                    api.post(`control/judge/${gate.gate}/activate`, {
                      version: other.version,
                    }),
                  `v${other.version} is live again`,
                );
                if (ok) await onChanged();
              }}
            >
              Make v{other.version} live
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}

function Agreement({ rows }: { rows: JudgeData["agreement"] }) {
  return (
    <div className="cc-card">
      <h3>Agreement with you</h3>
      <span className="sub">
        How often Jev&apos;s recommendation matched your decision, per kind of
        action: the labelled examples behind calibration.
      </span>
      {rows.length ? (
        <div className="tbl">
          {rows.map((r) => (
            <div
              key={r.action_key}
              className="row"
              style={{ gridTemplateColumns: "minmax(0,1fr) 70px 70px" }}
            >
              <span>{r.action_key.replace(/[:_]/g, " ")}</span>
              <span className="num">{r.decided} decided</span>
              <b className="num">
                {r.agreement === null
                  ? "–"
                  : `${Math.round(r.agreement * 100)}%`}
              </b>
            </div>
          ))}
        </div>
      ) : (
        <Empty title="No decisions yet">
          Agreement appears once you decide approvals that Jev recommended on.
        </Empty>
      )}
    </div>
  );
}
