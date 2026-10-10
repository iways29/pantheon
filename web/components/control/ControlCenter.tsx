"use client";

/**
 * The Control Center (Step 11, ADR 039): where the owner changes how the
 * company works, without code. A list of sections, so a new one is a new
 * entry here, not a redesign. The section is kept in the address (#prompts),
 * so a reload or a shared link opens the same screen.
 */

import { useEffect, useState, type ComponentType } from "react";

import { Agents } from "@/components/control/Agents";
import { Autonomy } from "@/components/control/Autonomy";
import { Departments } from "@/components/control/Departments";
import { Judge } from "@/components/control/Judge";
import { ToastHost } from "@/components/control/kit";
import { Knowledge } from "@/components/control/Knowledge";
import { Log } from "@/components/control/Log";
import { Mail } from "@/components/control/Mail";
import { Models } from "@/components/control/Models";
import { Prompts } from "@/components/control/Prompts";
import { Routines } from "@/components/control/Routines";
import { Rules } from "@/components/control/Rules";
import { Tasks } from "@/components/control/Tasks";
import { Tools } from "@/components/control/Tools";
import { Header } from "@/components/Header";
import { Icon } from "@/components/Icon";
import { PulseStrip } from "@/components/PulseStrip";
import { useCompany } from "@/lib/company";

interface Section {
  key: string;
  title: string;
  view: ComponentType;
}

const GROUPS: [string, Section[]][] = [
  [
    "Company",
    [
      { key: "departments", title: "Departments", view: Departments },
      { key: "agents", title: "Agents", view: Agents },
      { key: "prompts", title: "Prompts", view: Prompts },
      { key: "routines", title: "Morning routine", view: Routines },
      { key: "knowledge", title: "Knowledge", view: Knowledge },
    ],
  ],
  [
    "Spend and safety",
    [
      { key: "models", title: "Models and spend", view: Models },
      { key: "tools", title: "Tools and MCP", view: Tools },
      { key: "autonomy", title: "Autonomy and limits", view: Autonomy },
      { key: "judge", title: "Judge", view: Judge },
      { key: "rules", title: "Standing rules", view: Rules },
    ],
  ],
  ["Outbound", [{ key: "mail", title: "Newsletter and email", view: Mail }]],
  [
    "Record",
    [
      { key: "tasks", title: "Tasks", view: Tasks },
      { key: "log", title: "Change log", view: Log },
    ],
  ],
];

const ALL = GROUPS.flatMap(([, items]) => items);

function fromHash(): string {
  const key =
    typeof window === "undefined" ? "" : window.location.hash.slice(1);
  return ALL.some((s) => s.key === key) ? key : "departments";
}

export function ControlCenter() {
  const [key, setKey] = useState("departments");
  const { departments, agents, changedTools } = useCompany();

  useEffect(() => {
    setKey(fromHash());
    const onHash = () => setKey(fromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // On a phone the menu is one scrolling row: keep the open section in view.
  useEffect(() => {
    document
      .querySelector('.cc-nav [aria-current="page"]')
      ?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [key]);

  const counts: Record<string, number | undefined> = {
    departments: departments.length || undefined,
    agents: agents.length || undefined,
    tools: changedTools.length || undefined,
  };
  const section = ALL.find((s) => s.key === key) ?? ALL[0]!;
  const View = section.view;

  function go(next: string) {
    window.history.replaceState(null, "", `#${next}`);
    setKey(next);
  }

  return (
    <div className="screen cc">
      <Header tab="control" />
      <div className="cc-body">
        <nav
          className="glass panel cc-nav"
          aria-label="Control Center sections"
        >
          <div className="cc-nav-head" style={{ padding: "2px 12px 8px" }}>
            <div className="disp" style={{ fontSize: 19 }}>
              Control Center
            </div>
            <div
              className="faint"
              style={{ fontSize: 12, lineHeight: 1.4, marginTop: 3 }}
            >
              Every change is saved as a version and logged.
            </div>
          </div>
          {GROUPS.map(([group, items]) => (
            <div key={group} style={{ display: "contents" }}>
              <div className="navg">{group}</div>
              {items.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className="navi"
                  aria-current={item.key === key ? "page" : undefined}
                  onClick={() => go(item.key)}
                >
                  {item.title}
                  {counts[item.key] ? (
                    <span className="num">
                      {item.key === "tools" ? (
                        <span className="gl wait" aria-hidden="true" />
                      ) : null}
                      {counts[item.key]}
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          ))}
          <div className="lock" style={{ marginTop: "auto" }}>
            <Icon name="lock" size={14} />
            <span>Only you can pause, kill or decide an approval.</span>
          </div>
        </nav>
        <ToastHost>
          <main className="cc-main" aria-label={section.title}>
            <p className="phone-note lock">
              On the phone: switches, recipients and the log. Charters, prompts,
              the ladder and the judge are edited on a computer.
            </p>
            <View />
          </main>
        </ToastHost>
      </div>
      <PulseStrip />
    </div>
  );
}
