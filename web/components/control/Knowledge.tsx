"use client";

/**
 * Knowledge (designs: CcKnowledge): documents with a scope, links previewed
 * before anything reaches the brain, and what each agent can read. Research
 * topics and sources live in the research routine (Morning routine).
 */

import { useState } from "react";

import type { CcAgent } from "@/components/control/Agents";
import {
  Empty,
  Head,
  HistoryButton,
  Lock,
  Problem,
  Tag,
  useLoad,
  useSave,
} from "@/components/control/kit";
import { api, upload } from "@/lib/api";
import { agentName, departmentName, when } from "@/lib/format";

interface Doc {
  id: string;
  title: string;
  scope: string;
  status: string;
  screening: string | null;
  chunks: number | null;
  bytes: number | null;
  content_type: string | null;
  source_kind: string;
  at: string;
  department: string | null;
  agent: string | null;
}

interface Preview {
  id: string;
  url: string;
  final_url: string | null;
  label: string;
  reasons: string[];
  claims: unknown[];
  status: string;
  results: unknown[];
}

function size(bytes: number | null): string {
  if (!bytes) return "";
  return bytes > 1_000_000
    ? `${(bytes / 1_000_000).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1000))} KB`;
}

function claimText(c: unknown): string {
  if (typeof c === "string") return c;
  if (c && typeof c === "object" && "claim" in c)
    return String((c as { claim: unknown }).claim);
  return JSON.stringify(c);
}

export function Knowledge() {
  const docs = useLoad<Doc[]>("control/documents", /^document_/);
  const agents = useLoad<CcAgent[]>("control/agents");
  return (
    <>
      <Head
        title="Knowledge"
        lede="What agents may read: documents by scope, and links you check before they reach the brain."
      >
        <HistoryButton prefix="document_" title="Knowledge" />
      </Head>
      <Problem text={docs.error} />
      <div className="cc-split wide-left">
        <div style={{ display: "grid", gap: 14 }}>
          <div className="cc-card">
            <h2>Documents</h2>
            {docs.data && !docs.data.length ? (
              <Empty title="No documents yet">
                Upload the company profile first: every agent reads company
                documents.
              </Empty>
            ) : (
              <div className="tbl scroll-x">
                <div
                  className="th"
                  style={{
                    gridTemplateColumns: "minmax(160px,1fr) 130px 100px 70px",
                  }}
                >
                  <span>Document</span>
                  <span>Scope</span>
                  <span>Screening</span>
                  <span>Chunks</span>
                </div>
                {(docs.data ?? []).map((d) => (
                  <div
                    key={d.id}
                    className="row"
                    style={{
                      gridTemplateColumns: "minmax(160px,1fr) 130px 100px 70px",
                      paddingTop: 6,
                      paddingBottom: 6,
                    }}
                  >
                    <span>
                      {d.title}
                      <span className="sub">
                        {when(d.at)} {size(d.bytes) ? `· ${size(d.bytes)}` : ""}
                      </span>
                    </span>
                    <span>
                      {d.scope === "company"
                        ? "Whole company"
                        : d.scope === "department"
                          ? departmentName(d.department)
                          : agentName(d.agent)}
                    </span>
                    <span>
                      <Tag
                        kind={
                          d.status === "clean"
                            ? "ok"
                            : d.status === "quarantined"
                              ? "bad"
                              : "flag"
                        }
                      >
                        {d.status === "review" ? "held for you" : d.status}
                      </Tag>
                    </span>
                    <span className="num">{d.chunks ?? "–"}</span>
                  </div>
                ))}
              </div>
            )}
            <Upload agents={agents.data ?? []} onDone={docs.reload} />
          </div>
          <Links agents={agents.data ?? []} />
        </div>
        <Reads agents={agents.data ?? []} />
      </div>
    </>
  );
}

function Upload({
  agents,
  onDone,
}: {
  agents: CcAgent[];
  onDone: () => Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [scope, setScope] = useState("company");
  const [where, setWhere] = useState("");
  const save = useSave();
  const depts = [
    ...new Set(agents.map((a) => a.department).filter(Boolean)),
  ] as string[];
  const payer =
    agents.find((a) => a.role_type === "chief_of_staff")?.name ??
    agents[0]?.name;

  async function send() {
    if (!file || !payer) return;
    const q = new URLSearchParams({
      title: title || file.name,
      filename: file.name,
      scope,
      processed_by: payer,
    });
    if (scope === "department") q.set("department", where);
    if (scope === "agent") q.set("agent", where);
    const done = await save.run(
      () =>
        upload<{ status: string; chunks: number; reasons: string[] }>(
          `documents?${q}`,
          file,
        ),
      `${title || file.name} uploaded and screened`,
    );
    if (done) {
      setFile(null);
      setTitle("");
      if (done.reasons.length)
        save.setError(`Screening: ${done.reasons.join("; ")}`);
      await onDone();
    }
  }

  return (
    <div
      className="fld"
      style={{ borderTop: "1px solid var(--line)", paddingTop: 12 }}
    >
      <span className="lbl">
        Upload a document (text, Markdown, PDF or Word)
      </span>
      <div className="fields">
        <input
          className="inp"
          type="file"
          aria-label="File"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        <input
          className="inp"
          placeholder="Title"
          aria-label="Title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <select
          className="inp"
          aria-label="Scope"
          value={scope}
          onChange={(e) => {
            setScope(e.target.value);
            setWhere("");
          }}
        >
          <option value="company">Whole company</option>
          <option value="department">One department</option>
          <option value="agent">One agent</option>
        </select>
        {scope !== "company" ? (
          <select
            className="inp"
            aria-label={scope === "department" ? "Department" : "Agent"}
            value={where}
            onChange={(e) => setWhere(e.target.value)}
          >
            <option value="">Pick one</option>
            {(scope === "department" ? depts : agents.map((a) => a.name)).map(
              (x) => (
                <option key={x} value={x}>
                  {scope === "department" ? departmentName(x) : agentName(x)}
                </option>
              ),
            )}
          </select>
        ) : null}
      </div>
      <Problem text={save.error} />
      <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
        <button
          type="button"
          className="btn pri"
          disabled={save.busy || !file || (scope !== "company" && !where)}
          onClick={() => void send()}
        >
          Upload and screen
        </button>
      </div>
    </div>
  );
}

function Links({ agents }: { agents: CcAgent[] }) {
  const [url, setUrl] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const save = useSave();
  const reader =
    agents.find((a) => a.name === "web-researcher")?.name ??
    agents.find((a) => a.role_type === "chief_of_staff")?.name;

  async function check() {
    if (!reader) return;
    const got = await save.run(() =>
      api.post<Preview>("links/preview", { url, agent: reader }),
    );
    if (got) setPreview(got);
  }

  async function push() {
    if (!preview) return;
    const got = await save.run(
      () => api.post<Preview>(`links/${preview.id}/push`),
      "Sent to the brain through its write gate",
    );
    if (got) setPreview(got);
  }

  return (
    <div className="cc-card">
      <h2>Add a link</h2>
      <form
        style={{ display: "flex", gap: 8 }}
        onSubmit={(e) => {
          e.preventDefault();
          void check();
        }}
      >
        <input
          className="inp"
          placeholder="https://…"
          aria-label="Link"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        <button
          type="submit"
          className="btn glass"
          disabled={save.busy || !url.startsWith("http")}
        >
          Preview
        </button>
      </form>
      <Problem text={save.error} />
      {preview ? (
        <div className="fld">
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Tag
              kind={
                preview.label === "clean"
                  ? "ok"
                  : preview.label === "quarantine"
                    ? "bad"
                    : "flag"
              }
            >
              {preview.label}
            </Tag>
            <span className="sub" style={{ margin: 0 }}>
              {preview.final_url ?? preview.url}
            </span>
          </span>
          {preview.reasons.length ? (
            <span className="sub">{preview.reasons.join("; ")}</span>
          ) : null}
          <span className="lbl">
            Facts it would add ({preview.claims.length})
          </span>
          <ul
            style={{
              margin: 0,
              paddingLeft: 18,
              fontSize: 13,
              lineHeight: 1.5,
            }}
          >
            {preview.claims.map((c, i) => (
              <li key={i}>{claimText(c)}</li>
            ))}
          </ul>
          {preview.status === "pushed" ? (
            <span className="okay">
              Pushed: {preview.results.length} results from the write gate.
            </span>
          ) : (
            <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
              <button
                type="button"
                className="btn pri"
                disabled={
                  save.busy ||
                  preview.label !== "clean" ||
                  !preview.claims.length
                }
                onClick={() => void push()}
              >
                Push to brain
              </button>
            </div>
          )}
          {preview.label !== "clean" ? (
            <Lock>
              Only a clean page can be pushed; this one was flagged by
              screening.
            </Lock>
          ) : null}
        </div>
      ) : (
        <span className="sub">
          Nothing reaches the brain until you press Push; every fact still
          passes the brain&apos;s write gate.
        </span>
      )}
    </div>
  );
}

function Reads({ agents }: { agents: CcAgent[] }) {
  const [agent, setAgent] = useState("");
  const reads = useLoad<{ documents: { title: string; scope: string }[] }>(
    agent ? `control/agents/${agent}/reads` : null,
  );
  return (
    <div className="cc-card">
      <h2>What can this agent read?</h2>
      <select
        className="inp"
        aria-label="Agent"
        value={agent}
        onChange={(e) => setAgent(e.target.value)}
      >
        <option value="">Pick an agent</option>
        {agents.map((a) => (
          <option key={a.name} value={a.name}>
            {agentName(a.name)}
          </option>
        ))}
      </select>
      <Problem text={reads.error} />
      {agent && reads.data ? (
        reads.data.documents.length ? (
          <ul
            style={{
              margin: 0,
              paddingLeft: 18,
              fontSize: 13,
              lineHeight: 1.6,
            }}
          >
            {reads.data.documents.map((d) => (
              <li key={d.title}>
                {d.title} <span className="faint">({d.scope})</span>
              </li>
            ))}
          </ul>
        ) : (
          <span className="faint">No documents it can read.</span>
        )
      ) : (
        <span className="sub">
          Shown exactly as the database shows it to that agent: company
          documents, its department&apos;s, and its own.
        </span>
      )}
    </div>
  );
}
