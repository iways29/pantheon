"use client";

/**
 * Models and spend (designs: CcModels). Each tier points at a model from
 * OpenRouter's live catalogue, company-wide, with optional overrides per
 * department; prices beside them, and spend against the budgets.
 */

import { useMemo, useState } from "react";

import {
  Head,
  HistoryButton,
  Lock,
  Meter,
  Problem,
  useLoad,
  useSave,
} from "@/components/control/kit";
import { api } from "@/lib/api";
import { useCompany } from "@/lib/company";
import { departmentName, money } from "@/lib/format";

interface Models {
  tiers: string[];
  fallback: Record<string, string>;
  assignments: {
    tier: string;
    model: string;
    department: string | null;
    at: string;
  }[];
  prices: Record<string, { in: number; out: number }>;
  spend: {
    today: number;
    week: number;
    month: number;
    this_month: number;
    daily_budget: number;
  };
}

interface CatalogueModel {
  id: string;
  name: string;
  in_usd_per_mtok: number;
  out_usd_per_mtok: number;
  tools?: boolean;
}

const TIER_WORDS: Record<string, string> = {
  cheap: "Most work: reading, sorting, first drafts",
  standard: "Writing and harder judgement",
  frontier: "Only when a task really needs it",
  embedding: "How facts are placed by meaning",
};

/** The owner's ceiling for phase 1 (CLAUDE.md): $50–200 a month. */
const MONTHLY_CEILING = 200;

export function Models() {
  const data = useLoad<Models>("control/models", /^model_/);
  const catalogue = useLoad<CatalogueModel[]>("control/models/catalogue");
  const [dept, setDept] = useState("");
  const { departments } = useCompany();
  const m = data.data;
  const byId = useMemo(
    () => new Map((catalogue.data ?? []).map((c) => [c.id, c])),
    [catalogue.data],
  );
  const depts = [
    ...new Set((m?.assignments ?? []).map((a) => a.department).filter(Boolean)),
  ] as string[];

  return (
    <>
      <Head
        title="Models and spend"
        lede="Which model does the work at each tier, and what it costs. Changes apply from the next model call."
      >
        <HistoryButton prefix="model_" title="Models" />
      </Head>
      <Problem text={data.error ?? catalogue.error} />
      {m ? (
        <div className="cc-split wide-left">
          <div style={{ display: "grid", gap: 14 }}>
            <div className="cc-card">
              <div className="sech">
                <h2>Tiers</h2>
                <select
                  className="inp"
                  style={{ width: 200 }}
                  aria-label="Scope"
                  value={dept}
                  onChange={(e) => setDept(e.target.value)}
                >
                  <option value="">Whole company</option>
                  {[...departments.map((d) => d.name), ...depts]
                    .filter((v, i, a) => a.indexOf(v) === i)
                    .map((d) => (
                      <option key={d} value={d}>
                        {departmentName(d)} only
                      </option>
                    ))}
                </select>
              </div>
              {m.tiers.map((tier) => (
                <TierRow
                  key={`${dept}:${tier}`}
                  tier={tier}
                  dept={dept || null}
                  models={m}
                  catalogue={catalogue.data ?? []}
                  byId={byId}
                  onChanged={data.reload}
                />
              ))}
              <Lock>
                The embedding model is locked: changing it means placing every
                fact again. Ask first.
              </Lock>
            </div>
          </div>
          <Spend spend={m.spend} />
        </div>
      ) : (
        <span className="faint">Loading</span>
      )}
    </>
  );
}

function TierRow({
  tier,
  dept,
  models,
  catalogue,
  byId,
  onChanged,
}: {
  tier: string;
  dept: string | null;
  models: Models;
  catalogue: CatalogueModel[];
  byId: Map<string, CatalogueModel>;
  onChanged: () => Promise<void>;
}) {
  const company =
    models.assignments.find((a) => a.tier === tier && !a.department)?.model ??
    models.fallback[tier] ??
    "";
  const own = dept
    ? models.assignments.find((a) => a.tier === tier && a.department === dept)
        ?.model
    : undefined;
  const current = own ?? company;
  const [choice, setChoice] = useState(current);
  const save = useSave();
  const price = byId.get(current) ?? null;
  const known = models.prices[current];
  const locked = tier === "embedding";

  async function set(model: string | null) {
    const done = await save.run(
      () => api.put("control/models", { tier, model, department: dept }),
      model
        ? `${tier} tier${dept ? ` for ${departmentName(dept)}` : ""} now ${model}`
        : `${departmentName(dept)} uses the company's ${tier} model again`,
    );
    if (done) await onChanged();
  }

  return (
    <div
      className="fld"
      style={{ borderTop: "1px solid var(--line)", paddingTop: 12 }}
    >
      <div className="sech">
        <span>
          <b style={{ textTransform: "capitalize" }}>{tier}</b>
          <span className="sub">{TIER_WORDS[tier]}</span>
        </span>
        <span className="cost">
          {price ? (
            <>
              <span>
                In <b>${price.in_usd_per_mtok}</b>/M
              </span>
              <span>
                Out <b>${price.out_usd_per_mtok}</b>/M
              </span>
            </>
          ) : known ? (
            <span>
              In <b>${known.in}</b>/M
            </span>
          ) : null}
        </span>
      </div>
      {locked ? (
        <span className="code">{current || "not set"}</span>
      ) : (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <input
            className="inp code"
            style={{ flex: "1 1 260px" }}
            list={`models-${tier}`}
            aria-label={`${tier} model`}
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
          />
          <datalist id={`models-${tier}`}>
            {catalogue.slice(0, 600).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} · ${c.in_usd_per_mtok}/${c.out_usd_per_mtok}
              </option>
            ))}
          </datalist>
          <button
            type="button"
            className="btn sm pri"
            disabled={save.busy || !choice || choice === current}
            onClick={() => void set(choice)}
          >
            Use it
          </button>
          {dept && own ? (
            <button
              type="button"
              className="btn sm glass"
              disabled={save.busy}
              onClick={() => void set(null)}
            >
              Use company&apos;s
            </button>
          ) : null}
        </div>
      )}
      {dept ? (
        <span className="sub">
          {own
            ? `Override for ${departmentName(dept)}`
            : `Company-wide: ${company || "not set"}`}
        </span>
      ) : null}
      {choice && choice !== current && catalogue.length && !byId.has(choice) ? (
        <span className="err">Not in OpenRouter&apos;s catalogue</span>
      ) : null}
      {choice && byId.get(choice) && byId.get(choice)!.tools === false ? (
        <span className="sub">
          This model does not list tool use; agents with tools need it.
        </span>
      ) : null}
      <Problem text={save.error} />
    </div>
  );
}

function Spend({ spend }: { spend: Models["spend"] }) {
  const monthBudget = spend.daily_budget * 30;
  return (
    <div className="cc-card">
      <h2>Spend</h2>
      <div className="fields">
        <div className="fld">
          <span className="lbl">Today</span>
          <b className="num" style={{ fontSize: 20 }}>
            {money(spend.today)}
          </b>
          <Meter value={spend.today} of={spend.daily_budget} />
          <span className="sub">
            of {money(spend.daily_budget)} budgeted a day
          </span>
        </div>
        <div className="fld">
          <span className="lbl">7 days</span>
          <b className="num" style={{ fontSize: 20 }}>
            {money(spend.week)}
          </b>
        </div>
        <div className="fld">
          <span className="lbl">30 days</span>
          <b className="num" style={{ fontSize: 20 }}>
            {money(spend.month)}
          </b>
        </div>
      </div>
      <div className="fld">
        <span className="lbl">This month against your ceiling</span>
        <Meter value={spend.this_month} of={MONTHLY_CEILING} />
        <span className="sub">
          {money(spend.this_month)} of {money(MONTHLY_CEILING)} a month. Budgets
          as set allow up to {money(monthBudget)}.
        </span>
      </div>
      <Lock>
        Budgets are per department (Departments) and optionally per agent
        (Agents). Work stops for the day when a budget is spent.
      </Lock>
    </div>
  );
}
