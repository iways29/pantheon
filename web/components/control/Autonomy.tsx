"use client";

/**
 * Autonomy and limits (designs: CcAutonomy): the ladder of what each level
 * may do at each risk, promotions the approval history supports, the safety
 * limits, and Pause and Kill (ADR 022, 023).
 */

import { useEffect, useState } from "react";

import type { CcAgent } from "@/components/control/Agents";
import { RISK_WORDS } from "@/components/control/Tools";
import {
  Field,
  Head,
  HistoryButton,
  Lock,
  modeWords,
  Problem,
  useLoad,
  useSave,
} from "@/components/control/kit";
import { Icon } from "@/components/Icon";
import { KillDialog } from "@/components/KillDialog";
import { api } from "@/lib/api";
import { isKilled, useCompany } from "@/lib/company";
import { agentName } from "@/lib/format";

interface Suggestion {
  agent: string;
  action_key: string;
  risk_class: string;
  recommended: number;
  agreed: number;
  agreement: number | null;
  current_level: string;
  suggested_level: string | null;
  eligible: boolean;
}

interface AutonomyData {
  rules: { level: string; risk_class: string; mode: string }[];
  limits: Record<string, number>;
  suggestions: Suggestion[];
}

const LEVELS = ["L0", "L1", "L2", "L3"];
const RISKS = ["R0", "R1", "R2", "R3"];
const NEXT: Record<string, string> = { run: "gate", gate: "hold", hold: "run" };

const LIMIT_WORDS: [string, string, string][] = [
  ["max_depth", "How deep work can be handed down", "levels"],
  ["max_children", "Sub-tasks per task", "at most"],
  [
    "max_tasks_per_department_per_day",
    "Tasks per department per day",
    "at most",
  ],
  ["max_tasks_per_agent_per_hour", "Tasks per agent per hour", "at most"],
  ["loop_repeat_limit", "Same tool call repeated before stopping", "times"],
  ["stuck_task_minutes", "A task counts as stuck after", "minutes"],
  ["promotion_min_decisions", "Promotion needs at least", "decisions"],
  [
    "promotion_min_agreement",
    "and agreement with you of at least",
    "(0.5 to 1)",
  ],
];

export function Autonomy() {
  const data = useLoad<AutonomyData>(
    "control/autonomy",
    /^(autonomy_|delegation_|agent_|approval_decided)/,
  );
  const agents = useLoad<CcAgent[]>("control/agents", /^agent_/);
  const save = useSave();
  const [ignored, setIgnored] = useState<Set<string>>(new Set());
  const d = data.data;

  async function cycle(level: string, risk: string, mode: string) {
    const next = NEXT[mode] ?? "hold";
    await save.run(
      () =>
        api.put("control/autonomy/rules", {
          level,
          risk_class: risk,
          mode: next,
        }),
      `${level} at ${risk}: ${modeWords(next)}`,
    );
    await data.reload();
  }

  async function promote(s: Suggestion) {
    if (!s.suggested_level) return;
    await save.run(
      () =>
        api.post(`agents/${s.agent}/autonomy`, { level: s.suggested_level }),
      `${agentName(s.agent)} is now ${s.suggested_level}`,
    );
    await Promise.all([data.reload(), agents.reload()]);
  }

  const suggestions = (d?.suggestions ?? []).filter(
    (s) =>
      s.eligible &&
      s.suggested_level &&
      !ignored.has(`${s.agent}:${s.action_key}`),
  );

  return (
    <>
      <Head
        title="Autonomy and limits"
        lede="What agents may do alone, and the limits that keep work small and safe."
      >
        <HistoryButton
          prefix="autonomy_,delegation_,agent_updated,paused,unpaused,killed"
          title="Autonomy"
        />
      </Head>
      <Problem text={data.error ?? save.error} />
      <PauseKill />
      {d ? (
        <>
          <div className="cc-card desk-only">
            <h2>The ladder</h2>
            <span className="sub">
              For each level and risk: runs alone, Jev checks first, or asks
              you. Tap a cell to change it.
            </span>
            <div className="scroll-x">
              <div
                className="grid"
                style={{
                  gridTemplateColumns: `50px repeat(5, minmax(120px, 1fr))`,
                  minWidth: 680,
                }}
              >
                <span />
                {[...RISKS, "R4"].map((r) => (
                  <span key={r} className="faint" title={RISK_WORDS[r]}>
                    <b>{r}</b>{" "}
                    <span style={{ fontSize: 11 }}>
                      {RISK_WORDS[r]?.split(";")[0]}
                    </span>
                  </span>
                ))}
                {LEVELS.map((level) => (
                  <Ladder
                    key={level}
                    level={level}
                    rules={d.rules}
                    onCycle={cycle}
                    busy={save.busy}
                  />
                ))}
              </div>
            </div>
            <Lock>
              <b>R4 always asks you.</b> It spends money or acts for good; no
              level runs it alone.
            </Lock>
          </div>
          <div className="cc-split">
            <div style={{ display: "grid", gap: 14 }}>
              <Limits limits={d.limits} onChanged={data.reload} />
            </div>
            <div style={{ display: "grid", gap: 14 }}>
              <div className="cc-card">
                <h2>Promotions</h2>
                {suggestions.length ? (
                  suggestions.map((s) => (
                    <div
                      key={`${s.agent}:${s.action_key}`}
                      className="row"
                      style={{
                        gridTemplateColumns: "minmax(0,1fr) auto",
                        paddingTop: 8,
                        paddingBottom: 8,
                      }}
                    >
                      <span>
                        <b>{agentName(s.agent)}</b>: {s.recommended} decisions
                        on {s.action_key.replace(/[:_]/g, " ")},{" "}
                        {Math.round((s.agreement ?? 0) * 100)}% agreement with
                        you. Suggest {s.current_level} →{" "}
                        <b>{s.suggested_level}</b>.
                      </span>
                      <span className="cc-actions">
                        <button
                          type="button"
                          className="btn sm glass"
                          onClick={() =>
                            setIgnored(
                              new Set(ignored).add(
                                `${s.agent}:${s.action_key}`,
                              ),
                            )
                          }
                        >
                          Ignore
                        </button>
                        <button
                          type="button"
                          className="btn sm pri"
                          disabled={save.busy}
                          onClick={() => void promote(s)}
                        >
                          Accept
                        </button>
                      </span>
                    </div>
                  ))
                ) : (
                  <span className="faint" style={{ fontSize: 13 }}>
                    No promotion yet: an agent needs{" "}
                    {d.limits.promotion_min_decisions ?? 30} decisions with at
                    least{" "}
                    {Math.round(
                      (d.limits.promotion_min_agreement ?? 0.95) * 100,
                    )}
                    % agreement with you.
                  </span>
                )}
              </div>
              <div className="cc-card">
                <h2>Levels</h2>
                <span className="sub">
                  Change an agent&apos;s level on Agents. L0 asks for
                  everything; L3 acts alone up to R3.
                </span>
                <div className="tbl">
                  {(agents.data ?? []).map((a) => (
                    <div
                      key={a.name}
                      className="row"
                      style={{ gridTemplateColumns: "minmax(0,1fr) 50px" }}
                    >
                      <span>{agentName(a.name)}</span>
                      <b>{a.level}</b>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </>
      ) : null}
    </>
  );
}

function Ladder({
  level,
  rules,
  onCycle,
  busy,
}: {
  level: string;
  rules: AutonomyData["rules"];
  onCycle: (level: string, risk: string, mode: string) => Promise<void>;
  busy: boolean;
}) {
  return (
    <>
      <b>{level}</b>
      {RISKS.map((risk) => {
        const mode =
          rules.find((r) => r.level === level && r.risk_class === risk)?.mode ??
          "hold";
        return (
          <button
            key={risk}
            type="button"
            className={`cell ${mode}`}
            disabled={busy}
            aria-label={`${level} at ${risk}: ${modeWords(mode)}. Change`}
            onClick={() => void onCycle(level, risk, mode)}
          >
            <span className="m" aria-hidden="true" />
            {modeWords(mode)}
          </button>
        );
      })}
      <span
        className="cell hold lockd"
        aria-label={`${level} at R4: asks you, locked`}
      >
        <span className="m" aria-hidden="true" />
        asks you
      </span>
    </>
  );
}

function Limits({
  limits,
  onChanged,
}: {
  limits: Record<string, number>;
  onChanged: () => Promise<void>;
}) {
  const [form, setForm] = useState<Record<string, string>>({});
  useEffect(
    () =>
      setForm(
        Object.fromEntries(
          Object.entries(limits).map(([k, v]) => [k, String(v)]),
        ),
      ),
    [limits],
  );
  const save = useSave();
  const changed = Object.entries(form).filter(
    ([k, v]) => Number(v) !== limits[k],
  );

  async function apply() {
    const body = Object.fromEntries(changed.map(([k, v]) => [k, Number(v)]));
    const done = await save.run(
      () => api.put("control/autonomy/limits", body),
      "Limits saved",
    );
    if (done) await onChanged();
  }

  return (
    <div className="cc-card">
      <h2>Limits</h2>
      <div
        className="fields"
        style={{ gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))" }}
      >
        {LIMIT_WORDS.map(([key, label, unit]) => (
          <Field key={key} label={label} hint={unit}>
            <input
              className="inp"
              inputMode="decimal"
              value={form[key] ?? ""}
              onChange={(e) => setForm({ ...form, [key]: e.target.value })}
            />
          </Field>
        ))}
      </div>
      <Problem text={save.error} />
      <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
        <button
          type="button"
          className="btn pri"
          disabled={save.busy || !changed.length}
          onClick={() => void apply()}
        >
          Save limits
        </button>
      </div>
    </div>
  );
}

/** The same Pause and Kill as the brain's header (ADR 023). */
function PauseKill() {
  const { status, setPause } = useCompany();
  const [killing, setKilling] = useState(false);
  const save = useSave();
  const paused = status?.state === "paused";
  const killed = isKilled(status);
  return (
    <div
      className="cc-card"
      style={{ flexDirection: "row", alignItems: "center", flexWrap: "wrap" }}
    >
      <span style={{ flex: "1 1 240px" }}>
        <b>{killed ? "Stopped" : paused ? "Paused" : "Running"}</b>
        <span className="sub">
          Pause stops everything where it is; resume carries on. Kill cancels
          every unfinished task, run and held action for good.
        </span>
      </span>
      {paused ? (
        <button
          type="button"
          className="btn pri"
          disabled={save.busy}
          onClick={() => void save.run(() => setPause(false), "Resumed")}
        >
          <Icon name="play" />
          Resume
        </button>
      ) : (
        <button
          type="button"
          className="btn glass"
          disabled={save.busy || !status}
          onClick={() =>
            void save.run(() => setPause(true), "Everything paused")
          }
        >
          <Icon name="pause" />
          Pause
        </button>
      )}
      <button
        type="button"
        className="btn kill"
        disabled={save.busy || killed || !status}
        onClick={() =>
          void save.run(async () => {
            if (!paused) await setPause(true);
            setKilling(true);
          })
        }
      >
        <Icon name="kill" />
        Kill
      </button>
      {killing ? <KillDialog onClose={() => setKilling(false)} /> : null}
    </div>
  );
}
