"use client";

/**
 * Standing rules (designs: CcRules): your rules in plain English, which
 * every agent reads with the brain. Written here or said in chat and sorted
 * as a rule. Retiring one keeps it on record but stops it being recalled.
 */

import { useState } from "react";

import {
  Empty,
  Head,
  HistoryButton,
  Problem,
  Tag,
  useLoad,
  useSave,
} from "@/components/control/kit";
import { api } from "@/lib/api";
import { when } from "@/lib/format";

interface Rule {
  id: string;
  text: string;
  status: string;
  from: "written" | "chat";
  at: string;
  changed_at: string;
}

export function Rules() {
  const rules = useLoad<Rule[]>("control/rules", /^(rule_|fact_write_decided)/);
  const [text, setText] = useState("");
  const save = useSave();
  const live = (rules.data ?? []).filter((r) => r.status !== "retired");
  const retired = (rules.data ?? []).filter((r) => r.status === "retired");

  async function add() {
    const out = await save.run(() =>
      api.post<{ outcome: string; reasons: string[] }>("control/rules", {
        text,
      }),
    );
    if (out) {
      if (
        out.outcome === "accepted" ||
        out.outcome === "noted" ||
        out.outcome === "duplicate"
      ) {
        setText("");
        save.setError(null);
      } else
        save.setError(`Not stored (${out.outcome}): ${out.reasons.join("; ")}`);
      await rules.reload();
    }
  }

  async function retire(r: Rule) {
    await save.run(
      () => api.post(`control/rules/${r.id}/retire`),
      "Rule retired",
    );
    await rules.reload();
  }

  return (
    <>
      <Head
        title="Standing rules"
        lede="Your rules in plain English. Every agent reads them with the brain, and Jev weighs actions against them."
      >
        <HistoryButton prefix="rule_" title="Standing rules" />
      </Head>
      <form
        className="cc-card"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <h3>Add a rule</h3>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            className="inp"
            placeholder="e.g. No research on crypto tokens"
            aria-label="New rule"
            value={text}
            maxLength={500}
            onChange={(e) => setText(e.target.value)}
          />
          <button
            type="submit"
            className="btn pri"
            disabled={save.busy || text.trim().length < 5}
          >
            Add
          </button>
        </div>
        <span className="sub">
          It goes through the brain&apos;s write gate as your own words.
        </span>
        <Problem text={save.error ?? rules.error} />
      </form>
      <div className="cc-card">
        <h2>In force ({live.length})</h2>
        {live.length ? (
          <div className="tbl">
            {live.map((r) => (
              <div
                key={r.id}
                className="row"
                style={{
                  gridTemplateColumns: "minmax(0,1fr) auto auto",
                  paddingTop: 8,
                  paddingBottom: 8,
                }}
              >
                <span>
                  {r.text}
                  <span className="sub">
                    {r.from === "chat" ? "Said in chat" : "Written here"},{" "}
                    {when(r.at)}
                  </span>
                </span>
                {r.status === "disputed" ? (
                  <Tag kind="flag">disputed</Tag>
                ) : (
                  <Tag kind="live">in force</Tag>
                )}
                <button
                  type="button"
                  className="btn sm glass"
                  disabled={save.busy}
                  onClick={() => void retire(r)}
                >
                  Retire
                </button>
              </div>
            ))}
          </div>
        ) : rules.data ? (
          <Empty title="No rules yet">
            Add the first: a line you would tell a new hire, such as who never
            to email.
          </Empty>
        ) : null}
      </div>
      {retired.length ? (
        <div className="cc-card">
          <h3>Retired ({retired.length})</h3>
          {retired.map((r) => (
            <span key={r.id} className="faint" style={{ fontSize: 13 }}>
              {r.text} · retired {when(r.changed_at)}
            </span>
          ))}
        </div>
      ) : null}
    </>
  );
}
