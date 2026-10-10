"use client";

/**
 * Newsletter and email (designs: CcMail, ADR 028): the mailing lists and
 * the outbox. Recipients up to 50; a warning before a list with people other
 * than you sends on its own.
 */

import { useEffect, useState } from "react";

import {
  Dialog,
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

interface MailingList {
  key: string;
  name: string;
  from_address: string;
  reply_to: string | null;
  recipients: string[];
  subject: string;
  timezone: string;
  send_without_approval: boolean;
  enabled: boolean;
  approval_links_url: string | null;
  approval_link_hours: number;
  request_routines: string[];
}

interface Email {
  id: string;
  list_key: string;
  status: string;
  recipients: string[];
  subject: string;
  error: string | null;
  attempts: number;
  created_at: string;
  sent_at: string | null;
}

const EMAIL = /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/;

const STATUS_WORDS: Record<string, string> = {
  held: "Waiting for approval",
  ready: "Ready",
  sending: "Sending",
  sent: "Sent",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function Mail() {
  const lists = useLoad<MailingList[]>("mailing-lists", /^mailing_list_/);
  const emails = useLoad<Email[]>("emails?limit=30", /^email_/);
  const [chosen, setChosen] = useState<string | null>(null);
  useEffect(() => {
    if (!chosen && lists.data?.length) setChosen(lists.data[0]!.key);
  }, [chosen, lists.data]);
  const picked = lists.data?.find((l) => l.key === chosen) ?? null;
  return (
    <>
      <Head
        title="Newsletter and email"
        lede="Who gets which email, and what went out."
      >
        <HistoryButton prefix="mailing_list_" title="Mailing lists" />
      </Head>
      <Problem text={lists.error ?? emails.error} />
      <div className="cc-split">
        <div style={{ display: "grid", gap: 14 }}>
          <div className="tbl">
            {(lists.data ?? []).map((l) => (
              <button
                key={l.key}
                type="button"
                className={`row${l.key === chosen ? " sel" : ""}`}
                style={{ gridTemplateColumns: "minmax(0,1fr) auto" }}
                onClick={() => setChosen(l.key)}
              >
                <span>
                  {l.name}
                  <span className="sub">
                    {l.recipients.length} recipients ·{" "}
                    {l.send_without_approval
                      ? "sends on its own"
                      : "waits for approval"}
                  </span>
                </span>
                <Tag kind={l.enabled ? "live" : "off"}>
                  {l.enabled ? "On" : "Off"}
                </Tag>
              </button>
            ))}
          </div>
          <Outbox emails={emails.data ?? []} onChanged={emails.reload} />
        </div>
        {picked ? (
          <ListCard key={picked.key} list={picked} onChanged={lists.reload} />
        ) : null}
      </div>
    </>
  );
}

function ListCard({
  list,
  onChanged,
}: {
  list: MailingList;
  onChanged: () => Promise<void>;
}) {
  const [form, setForm] = useState({
    name: list.name,
    from_address: list.from_address,
    reply_to: list.reply_to ?? "",
    subject: list.subject,
    timezone: list.timezone,
  });
  const [people, setPeople] = useState(list.recipients);
  const [adding, setAdding] = useState("");
  const [confirmAuto, setConfirmAuto] = useState(false);
  const save = useSave();
  const others = people.filter(
    (p) => p.toLowerCase() !== list.from_address.toLowerCase(),
  );

  async function put(change: Record<string, unknown>, done: string) {
    const ok = await save.run(
      () => api.put(`mailing-lists/${list.key}`, change),
      done,
    );
    if (ok) await onChanged();
  }

  function addPerson() {
    const v = adding.trim().toLowerCase();
    if (!EMAIL.test(v)) {
      save.setError(`${v || "That"} is not an email address.`);
      return;
    }
    if (people.length >= 50) {
      save.setError("A list holds up to 50 people.");
      return;
    }
    if (!people.includes(v)) setPeople([...people, v]);
    setAdding("");
    save.setError(null);
  }

  return (
    <div className="cc-card">
      <div className="sech">
        <h2>{list.name}</h2>
        <Toggle
          on={list.enabled}
          label="List on or off"
          disabled={save.busy}
          onChange={(on) =>
            void put(
              { enabled: on },
              `${list.name} switched ${on ? "on" : "off"}`,
            )
          }
        />
      </div>
      <div className="fields">
        <Field label="Name">
          <input
            className="inp"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </Field>
        <Field label="Sender">
          <input
            className="inp"
            value={form.from_address}
            onChange={(e) => setForm({ ...form, from_address: e.target.value })}
          />
        </Field>
        <Field label="Reply to">
          <input
            className="inp"
            value={form.reply_to}
            onChange={(e) => setForm({ ...form, reply_to: e.target.value })}
          />
        </Field>
        <Field label="Subject" hint="{date} becomes the day it is written">
          <input
            className="inp"
            value={form.subject}
            onChange={(e) => setForm({ ...form, subject: e.target.value })}
          />
        </Field>
        <Field label="Time zone">
          <input
            className="inp"
            value={form.timezone}
            onChange={(e) => setForm({ ...form, timezone: e.target.value })}
          />
        </Field>
      </div>
      <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
        <button
          type="button"
          className="btn glass"
          disabled={save.busy}
          onClick={() =>
            void put({ ...form, reply_to: form.reply_to || null }, "List saved")
          }
        >
          Save details
        </button>
      </div>
      <div className="fld">
        <span className="lbl">Recipients ({people.length} of 50)</span>
        <div className="chipset">
          {people.map((p) => (
            <button
              key={p}
              type="button"
              className="chip"
              aria-label={`Remove ${p}`}
              onClick={() => setPeople(people.filter((x) => x !== p))}
            >
              {p} ×
            </button>
          ))}
        </div>
        <form
          style={{ display: "flex", gap: 8 }}
          onSubmit={(e) => {
            e.preventDefault();
            addPerson();
          }}
        >
          <input
            className="inp"
            placeholder="name@example.com"
            aria-label="Add a recipient"
            value={adding}
            onChange={(e) => setAdding(e.target.value)}
          />
          <button type="submit" className="btn sm glass">
            Add
          </button>
        </form>
        <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
          <button
            type="button"
            className="btn pri"
            disabled={save.busy || people.join() === list.recipients.join()}
            onClick={() => void put({ recipients: people }, "Recipients saved")}
          >
            Save recipients
          </button>
        </div>
      </div>
      <div className="fld">
        <span className="lbl">Sending</span>
        <Toggle
          on={list.send_without_approval}
          label="Sends on its own"
          words={["Sends on its own", "Each email waits for your approval"]}
          disabled={save.busy}
          onChange={(on) =>
            on && others.length
              ? setConfirmAuto(true)
              : void put(
                  { send_without_approval: on },
                  on ? "Sends on its own" : "Each email waits for you",
                )
          }
        />
      </div>
      {list.approval_links_url ? (
        <span className="sub">
          Emails end with one-tap approval links ({list.approval_link_hours}{" "}
          hours)
          {list.request_routines.length
            ? " and the “Ask for tomorrow” form"
            : ""}
          .
        </span>
      ) : null}
      {others.length && list.approval_links_url ? (
        <Lock>
          <b>Others are on this list.</b> Anyone with the email can use its
          approval links and topic form; switch them off before adding people
          (scripts.mailing set {list.key} --no-links --no-requests).
        </Lock>
      ) : null}
      <Problem text={save.error} />
      {confirmAuto ? (
        <Dialog title="Send on its own?" onClose={() => setConfirmAuto(false)}>
          <p style={{ margin: 0, lineHeight: 1.5 }}>
            {others.length} {others.length === 1 ? "person" : "people"} besides
            you will get these emails with no approval from you first.
          </p>
          <div className="cc-actions" style={{ justifyContent: "flex-end" }}>
            <button
              type="button"
              className="btn glass"
              onClick={() => setConfirmAuto(false)}
            >
              Keep waiting for me
            </button>
            <button
              type="button"
              className="btn pri"
              onClick={async () => {
                setConfirmAuto(false);
                await put({ send_without_approval: true }, "Sends on its own");
              }}
            >
              Send on its own
            </button>
          </div>
        </Dialog>
      ) : null}
    </div>
  );
}

function Outbox({
  emails,
  onChanged,
}: {
  emails: Email[];
  onChanged: () => Promise<void>;
}) {
  const save = useSave();
  return (
    <div className="cc-card">
      <h3>Outbox</h3>
      <Problem text={save.error} />
      <div className="tbl">
        {emails.map((e) => (
          <div
            key={e.id}
            className="row"
            style={{
              gridTemplateColumns: "minmax(0,1fr) auto",
              paddingTop: 8,
              paddingBottom: 8,
            }}
          >
            <span>
              {e.subject}
              <span className="sub">
                {STATUS_WORDS[e.status] ?? e.status} ·{" "}
                {when(e.sent_at ?? e.created_at)} · {e.recipients.length}{" "}
                recipients
              </span>
              {e.error ? <span className="err">{e.error}</span> : null}
            </span>
            {e.status === "failed" && e.attempts < 3 ? (
              <button
                type="button"
                className="btn sm glass"
                disabled={save.busy}
                onClick={async () => {
                  await save.run(
                    () => api.post(`emails/${e.id}/send`),
                    "Sent again",
                  );
                  await onChanged();
                }}
              >
                Retry
              </button>
            ) : (
              <Tag
                kind={
                  e.status === "sent"
                    ? "ok"
                    : e.status === "failed"
                      ? "bad"
                      : e.status === "held"
                        ? "flag"
                        : undefined
                }
              >
                {STATUS_WORDS[e.status] ?? e.status}
              </Tag>
            )}
          </div>
        ))}
      </div>
      {!emails.length ? <span className="faint">Nothing sent yet.</span> : null}
    </div>
  );
}
