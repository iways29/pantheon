"use client";

/**
 * Tasks: every task tree, newest first, with its status, cost and who
 * started it. Follow one on the brain to see its path.
 */

import { Empty, Head, Problem, Tag, useLoad } from "@/components/control/kit";
import { agentName, money, when } from "@/lib/format";

interface TaskTree {
  id: string;
  title: string;
  status: string;
  at: string;
  by: string | null;
  agent: string;
  cost_usd: number;
  steps: number;
}

const KIND: Record<string, string> = {
  done: "ok",
  failed: "bad",
  cancelled: "off",
  blocked: "flag",
  awaiting_approval: "flag",
};

function who(by: string | null): string {
  if (!by) return "unknown";
  if (by.startsWith("trigger:")) return "the morning routine";
  if (by === "owner" || /^[0-9a-f-]{36}$/.test(by)) return "you";
  return by;
}

export function Tasks() {
  const tasks = useLoad<TaskTree[]>("control/tasks?limit=80", /^task_/);
  return (
    <>
      <Head
        title="Tasks"
        lede="Every piece of work, newest first: who started it, where it stands, what it cost."
      />
      <Problem text={tasks.error} />
      {tasks.data && !tasks.data.length ? (
        <Empty title="No tasks yet">
          Give the Chief of Staff an order in the chat, or switch on a routine.
        </Empty>
      ) : null}
      {tasks.data?.length ? (
        <div className="tbl scroll-x">
          <div
            className="th"
            style={{
              gridTemplateColumns: "minmax(200px,1fr) 140px 80px 70px 120px",
            }}
          >
            <span>Task</span>
            <span>Status</span>
            <span>Steps</span>
            <span>Cost</span>
            <span>When</span>
          </div>
          {(tasks.data ?? []).map((t) => (
            <div
              key={t.id}
              className="row"
              style={{
                gridTemplateColumns: "minmax(200px,1fr) 140px 80px 70px 120px",
                paddingTop: 6,
                paddingBottom: 6,
              }}
            >
              <span>
                {t.title}
                <span className="sub">
                  {agentName(t.agent)}, started by {who(t.by)}
                </span>
              </span>
              <span>
                <Tag kind={KIND[t.status]}>{t.status.replace("_", " ")}</Tag>
              </span>
              <span className="num">{t.steps}</span>
              <span className="num">{money(t.cost_usd)}</span>
              <span className="faint">{when(t.at)}</span>
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}
