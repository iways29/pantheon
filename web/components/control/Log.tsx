"use client";

/**
 * The change log (designs: CcLog): every change to how the company works, in
 * plain words, newest first, filterable by screen.
 */

import { useState } from "react";

import {
  Head,
  LogList,
  Problem,
  useLoad,
  type LogEvent,
} from "@/components/control/kit";
import { api } from "@/lib/api";

const FILTERS: [string, string][] = [
  ["", "Everything"],
  ["charter_,department_", "Departments"],
  ["agent_", "Agents and prompts"],
  ["trigger_,routine_request_", "Morning routine"],
  ["document_", "Knowledge"],
  ["model_", "Models"],
  ["tool_,mcp_", "Tools"],
  ["autonomy_,delegation_,paused,unpaused,killed", "Autonomy and safety"],
  ["judge_", "Judge"],
  ["rule_,approval_decided", "Rules and decisions"],
  ["mailing_list_", "Email"],
];

export function Log() {
  const [filter, setFilter] = useState("");
  const log = useLoad<LogEvent[]>(
    `control/log?limit=100${filter ? `&prefix=${encodeURIComponent(filter)}` : ""}`,
    /./,
  );
  const [more, setMore] = useState<LogEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const all = [...(log.data ?? []), ...more];

  async function older() {
    const last = all[all.length - 1];
    if (!last) return;
    setBusy(true);
    try {
      const next = await api.get<LogEvent[]>(
        `control/log?limit=100&before=${encodeURIComponent(last.at)}${filter ? `&prefix=${encodeURIComponent(filter)}` : ""}`,
      );
      setMore([...more, ...next]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Head
        title="Change log"
        lede="Every change to how the company works: who made it and when. The work itself is on the brain."
      />
      <div className="chipset" role="group" aria-label="Filter">
        {FILTERS.map(([key, label]) => (
          <button
            key={label}
            type="button"
            className="chip"
            aria-pressed={filter === key}
            onClick={() => {
              setFilter(key);
              setMore([]);
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <Problem text={log.error} />
      <div className="cc-card">
        {log.data ? (
          <LogList events={all} />
        ) : (
          <span className="faint">Loading</span>
        )}
        {log.data && log.data.length === 100 ? (
          <button
            type="button"
            className="btn glass"
            disabled={busy}
            onClick={() => void older()}
            style={{ alignSelf: "center" }}
          >
            Older
          </button>
        ) : null}
      </div>
    </>
  );
}
