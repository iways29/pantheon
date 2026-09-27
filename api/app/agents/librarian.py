"""The `librarian` runner: the brain's graph (owner, 2026-09-27; ADR 035).

Each run reads facts not yet read (`facts.graph_state = 'pending'`), a batch
at a time, and for each one:

1. **Proposes.** One cheap-tier call, with the librarian's `extract_graph`
   prompt (data, like every prompt) and the allowed kinds and relations (the
   `graph_schema` flag), lists the things the fact is about and the links it
   states. Anything outside the lists is dropped.
2. **Matches.** Each thing is matched to what the brain already holds: same
   name or other name, or near by meaning. With candidates, Jev decides which
   one it is, or none (`entity_match`, candidates added as options). Sure: the
   mention joins the existing thing (a new name becomes another name for it).
   Unsure: a new thing marked "possible match" for the tidy-up. None: a new
   thing. So a fact arriving days later attaches to the things already there.
3. **Checks links.** Jev answers whether the fact states each link
   (`link_support`); only those it does are kept, each traced to the fact.

Before reading, facts not yet on the brain screen's map are placed (no model
call, ADR 036); after tidying, up to a few neighbourhoods of the map get a
name (one cheap call each, the `name_neighbourhood` prompt, when it exists).

Then **tidies**: things that are probably one (same or overlapping names,
very near by meaning, or marked "possible match") are put to Jev again and
merged when it is sure. A merge keeps the old thing, pointing at the
survivor, so it can be undone. Things with no links are counted in the
output, for the owner to see.

A fact the model cannot read is marked `skipped`, never retried forever.
Every change is an event (entities_audit, `graph_updated`).
"""

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg

from app.brain import layout
from app.gateway import GatewayError
from app.judge import JudgeError
from app.judge.store import load_gate

if TYPE_CHECKING:
    from app.agents.research import Session
    from app.agents.runs import _Run

MATCH_GATE = "entity_match"
LINK_GATE = "link_support"
PROMPT_SLOT = "extract_graph"
NAME_SLOT = "name_neighbourhood"
SCHEMA_FLAG = "graph_schema"
#: Facts read per run, things and links per fact, merges per tidy-up.
BATCH = 10
MAX_THINGS = 6
MAX_LINKS = 6
MAX_MERGES = 10
EXTRACT_MAX_TOKENS = 1500
#: Things this near by meaning, of one kind, are put to Jev as one thing.
TWIN_MAX_DISTANCE = 0.15


@dataclass(frozen=True)
class Schema:
    kinds: tuple[str, ...]
    relations: tuple[str, ...]


#: The starting kinds and relations, used when the org has no `graph_schema`
#: flag yet; the flag (data) wins once it exists.
DEFAULT_SCHEMA = Schema(
    kinds=("person", "company", "product", "project", "investor", "fund", "topic"),
    relations=(
        "founded",
        "works_on",
        "invested_in",
        "competes_with",
        "part_of",
        "decided_about",
        "prefers",
    ),
)


def run(session: "Session", run: "_Run") -> dict[str, Any]:
    cursor = session.connection.cursor()
    cursor.execute(
        "select body from public.agent_prompts where agent_id = %s and slot = %s and active",
        (str(run.agent_id), PROMPT_SLOT),
    )
    prompt = cursor.fetchone()
    if prompt is None:
        return _result(
            "failed", "prompt_missing", error=f"The librarian has no `{PROMPT_SLOT}` prompt"
        )
    if session.judge is None:
        return _result("failed", "no_judge", error="The graph needs Jev (TYPESAFE_API_KEY)")
    schema = _schema(cursor, run.org_id)
    try:
        match = load_gate(session.connection, org_id=run.org_id, gate=MATCH_GATE).policy
    except JudgeError as error:
        return _result("failed", "gate_missing", error=str(error))
    librarian = Librarian(session, run, prompt["body"], schema, match)
    placed = layout.fit(session.connection, run.org_id)["placed"]

    cursor.execute(
        "select id, claim from public.facts where org_id = %s and graph_state = 'pending' "
        "and status = 'active' order by created_at limit %s",
        (str(run.org_id), BATCH),
    )
    counts = {"facts": 0, "skipped": 0, "things_new": 0, "things_joined": 0, "links": 0}
    for fact in cursor.fetchall():
        read = librarian.read(fact)
        for key, value in read.items():
            counts[key] = counts.get(key, 0) + value
    counts["merged"] = librarian.tidy()
    counts["placed"] = placed
    counts["named"] = _name_neighbourhoods(session, run, cursor)
    cursor.execute(
        """
        select count(*) as n from public.entities e
         where e.org_id = %s and e.status = 'active'
           and not exists (select 1 from public.entity_links l where l.status = 'active'
                            and (l.from_entity = e.id or l.to_entity = e.id))
        """,
        (str(run.org_id),),
    )
    counts["unlinked_things"] = cursor.fetchone()["n"]
    cursor.execute(
        "select count(*) as n from public.facts where org_id = %s and graph_state = 'pending' "
        "and status = 'active'",
        (str(run.org_id),),
    )
    counts["still_pending"] = cursor.fetchone()["n"]
    cursor.execute(
        "insert into public.events (org_id, run_id, agent_id, type, payload) "
        "values (%s, %s, %s, 'graph_updated', %s)",
        (str(run.org_id), str(run.id), str(run.agent_id), json.dumps(counts)),
    )
    return _result("succeeded", "completed", output=counts)


class Librarian:
    def __init__(
        self,
        session: "Session",
        run: "_Run",
        prompt: str,
        schema: Schema,
        match: Any,  # noqa: ANN401
    ) -> None:
        self.s, self.run, self.prompt, self.schema, self.match = session, run, prompt, schema, match
        self.cursor = session.connection.cursor()

    # --- one fact -----------------------------------------------------------

    def read(self, fact: dict[str, Any]) -> dict[str, int]:
        proposed = self._propose(fact["claim"])
        if proposed is None:
            self._mark(fact["id"], "skipped")
            return {"skipped": 1}
        things, links = proposed
        counts = {"facts": 1, "things_new": 0, "things_joined": 0, "links": 0}
        resolved: dict[str, UUID] = {}
        for thing in things:
            entity_id, joined = self._resolve(thing, fact["claim"])
            resolved[thing["name"].lower()] = entity_id
            counts["things_joined" if joined else "things_new"] += 1
            self.cursor.execute(
                "insert into public.fact_entities (org_id, fact_id, entity_id) values (%s, %s, %s) "
                "on conflict do nothing",
                (str(self.run.org_id), str(fact["id"]), str(entity_id)),
            )
        for link in links:
            a = resolved.get(link["from"].lower())
            b = resolved.get(link["to"].lower())
            if a is None or b is None or a == b:
                continue
            confidence = self._supported(fact["claim"], link)
            if confidence is None:
                continue
            self.cursor.execute(
                """
                insert into public.entity_links
                    (org_id, from_entity, relation, to_entity, fact_id, confidence)
                values (%s, %s, %s, %s, %s, %s)
                on conflict do nothing
                """,
                (
                    str(self.run.org_id),
                    str(a),
                    link["relation"],
                    str(b),
                    str(fact["id"]),
                    round(confidence, 3),
                ),
            )
            counts["links"] += 1
        self._mark(fact["id"], "done")
        return counts

    def _propose(self, claim: str) -> tuple[list[dict[str, str]], list[dict[str, str]]] | None:
        # A provider error pauses the run and the batch is retried: the facts
        # stay pending (the run's transaction is rolled back).
        response = self.s.gateway.complete(
            agent_id=self.run.agent_id,
            run_id=self.run.id,
            max_tokens=EXTRACT_MAX_TOKENS,
            messages=[
                {"role": "system", "content": self.prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "fact": claim,
                            "kinds": list(self.schema.kinds),
                            "relations": list(self.schema.relations),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        return parse(response.text, self.schema)

    def _resolve(self, thing: dict[str, str], context: str) -> tuple[UUID, bool]:
        """The existing thing this is, or a new one. True when it joined one."""
        candidates = self._candidates(thing)
        if not candidates:
            return self._create(thing), False
        options = {f"c{i}": _describe(c) for i, c in enumerate(candidates, start=1)}
        try:
            decision = self.s.judge.run(
                MATCH_GATE,
                {
                    "mention": thing["name"],
                    "kind": thing["kind"],
                    "context": context,
                    "candidates": "\n".join(f"{k}: {v}" for k, v in options.items()),
                },
                agent_id=self.run.agent_id,
                run_id=self.run.id,
                extra_options={"match": options},
            )
        except (JudgeError, GatewayError):
            return self._create(thing), False
        if decision.failed:
            return self._create(thing), False
        answer = decision.answers["match"]
        best = max(options, key=lambda k: answer.probabilities.get(k, 0.0))
        p = answer.probabilities.get(best, 0.0)
        chosen = candidates[int(best[1:]) - 1]
        if answer.choice != "new" and p >= self.match.setting("join_at_least", 0.7):
            self._join(chosen["id"], thing)
            return chosen["id"], True
        if p >= self.match.setting("unsure_at_least", 0.4):
            return self._create(thing, possible_match_of=chosen["id"]), False
        return self._create(thing), False

    def _candidates(self, thing: dict[str, str]) -> list[dict[str, Any]]:
        limit = int(self.match.setting("candidates", 4))
        self.cursor.execute(
            """
            select id, kind, name, aliases, description from public.entities
             where org_id = %s and status = 'active'
               and (lower(name) = lower(%s)
                    or lower(%s) = any(select lower(a) from unnest(aliases) a))
             limit %s
            """,
            (str(self.run.org_id), thing["name"], thing["name"], limit),
        )
        found = [dict(r) for r in self.cursor.fetchall()]
        vector = self._embed(thing)
        if vector is not None and len(found) < limit:
            self.cursor.execute(
                """
                select id, kind, name, aliases, description from public.entities
                 where org_id = %s and status = 'active' and embedding is not null
                   and embedding_model = %s and embedding <=> %s::vector <= %s
                   and not (id = any(%s))
                 order by embedding <=> %s::vector
                 limit %s
                """,
                (
                    str(self.run.org_id),
                    self.s.embedder.name,
                    vector,
                    self.match.setting("candidate_max_distance", 0.5),
                    [str(f["id"]) for f in found],
                    vector,
                    limit - len(found),
                ),
            )
            found += [dict(r) for r in self.cursor.fetchall()]
        return found

    def _create(self, thing: dict[str, str], possible_match_of: UUID | None = None) -> UUID:
        vector = self._embed(thing)
        self.cursor.execute(
            """
            insert into public.entities
                (org_id, kind, name, description, embedding, embedding_model, possible_match_of)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                str(self.run.org_id),
                thing["kind"],
                thing["name"],
                thing.get("description", "")[:1000],
                vector,
                self.s.embedder.name if vector is not None else None,
                str(possible_match_of) if possible_match_of else None,
            ),
        )
        return self.cursor.fetchone()["id"]

    def _join(self, entity_id: UUID, thing: dict[str, str]) -> None:
        """Another mention of a thing: its name, when new, becomes another name."""
        self.cursor.execute(
            """
            update public.entities
               set aliases = case
                     when lower(%s) = lower(name)
                       or lower(%s) = any(select lower(a) from unnest(aliases) a) then aliases
                     else array_append(aliases, %s) end,
                   description = case when description = '' then %s else description end
             where id = %s
            """,
            (
                thing["name"],
                thing["name"],
                thing["name"],
                thing.get("description", "")[:1000],
                str(entity_id),
            ),
        )

    def _supported(self, claim: str, link: dict[str, str]) -> float | None:
        """Jev's yes-probability that the fact states the link, if kept."""
        text = f"{link['from']} {link['relation'].replace('_', ' ')} {link['to']}"
        try:
            decision = self.s.judge.run(
                LINK_GATE,
                {"text": claim, "link": text},
                agent_id=self.run.agent_id,
                run_id=self.run.id,
            )
        except (JudgeError, GatewayError):
            return None
        if decision.failed or decision.outcome != "keep":
            return None
        return float(decision.answers["states"].noul)

    def _embed(self, thing: dict[str, str]) -> Any:  # noqa: ANN401
        if self.s.embedder is None:
            return None
        vector = self.s.embedder.embed(
            f"{thing['name']} ({thing['kind']}): {thing.get('description', '')}"
        )
        return vector if any(vector) else None

    def _mark(self, fact_id: UUID, state: str) -> None:
        self.cursor.execute(
            "update public.facts set graph_state = %s where id = %s", (state, str(fact_id))
        )

    # --- tidy-up ------------------------------------------------------------

    def tidy(self) -> int:
        """Merge things Jev is sure are one. Returns how many were merged."""
        self.cursor.execute(
            """
            select a.id as a_id, b.id as b_id
              from public.entities a
              join public.entities b
                on b.org_id = a.org_id and b.kind = a.kind and b.id <> a.id
               and b.status = 'active' and a.created_at <= b.created_at
             where a.org_id = %s and a.status = 'active'
               and (
                 b.possible_match_of = a.id
                 or lower(a.name) = lower(b.name)
                 or lower(b.name) = any(select lower(x) from unnest(a.aliases) x)
                 or lower(a.name) = any(select lower(x) from unnest(b.aliases) x)
                 or (a.embedding is not null and b.embedding is not null
                     and a.embedding_model = b.embedding_model
                     and a.embedding <=> b.embedding <= %s)
               )
             order by a.created_at
             limit %s
            """,
            (str(self.run.org_id), TWIN_MAX_DISTANCE, MAX_MERGES * 2),
        )
        pairs = self.cursor.fetchall()
        merged = 0
        for pair in pairs:
            if merged >= MAX_MERGES:
                break
            a, b = self._entity(pair["a_id"]), self._entity(pair["b_id"])
            if a is None or b is None or a["status"] != "active" or b["status"] != "active":
                continue
            verdict = self._same(a, b)
            if verdict == "same":
                self._merge(survivor=a, loser=b)
                merged += 1
            elif verdict == "apart" and b.get("possible_match_of"):
                # Only a sure "different" clears the mark; an unsure pair is
                # looked at again on the next tidy-up.
                self.cursor.execute(
                    "update public.entities set possible_match_of = null where id = %s",
                    (str(b["id"]),),
                )
        return merged

    def _same(self, a: dict[str, Any], b: dict[str, Any]) -> str:
        """Same thing, apart (Jev is sure they differ), or unsure."""
        options = {"c1": _describe(a)}
        try:
            decision = self.s.judge.run(
                MATCH_GATE,
                {
                    "mention": b["name"],
                    "kind": b["kind"],
                    "context": b["description"] or b["name"],
                    "candidates": f"c1: {options['c1']}",
                },
                agent_id=self.run.agent_id,
                run_id=self.run.id,
                extra_options={"match": options},
            )
        except (JudgeError, GatewayError):
            return "unsure"
        if decision.failed:
            return "unsure"
        answer = decision.answers["match"]
        sure = self.match.setting("join_at_least", 0.7)
        if answer.probabilities.get(answer.choice, 0.0) < sure:
            return "unsure"
        return "same" if answer.choice == "c1" else "apart"

    def _merge(self, *, survivor: dict[str, Any], loser: dict[str, Any]) -> None:
        s, lo = str(survivor["id"]), str(loser["id"])
        c = self.cursor
        c.execute(
            "insert into public.fact_entities (org_id, fact_id, entity_id) "
            "select org_id, fact_id, %s from public.fact_entities where entity_id = %s "
            "on conflict do nothing",
            (s, lo),
        )
        # The loser keeps its own rows (and its leftover links, retired): a
        # wrong merge can be undone from them.
        for end, other in (("from_entity", "to_entity"), ("to_entity", "from_entity")):
            c.execute(
                f"""
                update public.entity_links l set {end} = %s
                 where l.{end} = %s and l.{other} <> %s
                   and not exists (
                     select 1 from public.entity_links d
                      where d.org_id = l.org_id and d.relation = l.relation
                        and d.fact_id = l.fact_id and d.{end} = %s and d.{other} = l.{other})
                """,
                (s, lo, s, s),
            )
        c.execute(
            "update public.entity_links set status = 'retired' "
            "where (from_entity = %s or to_entity = %s) and status = 'active'",
            (lo, lo),
        )
        names = sorted(
            {*survivor["aliases"], *loser["aliases"], loser["name"]} - {survivor["name"]}
        )
        c.execute(
            "update public.entities set aliases = %s, "
            "description = case when description = '' then %s else description end where id = %s",
            (names, loser["description"], s),
        )
        c.execute(
            "update public.entities set status = 'merged', merged_into = %s, "
            "possible_match_of = null where id = %s",
            (s, lo),
        )

    def _entity(self, entity_id: UUID) -> dict[str, Any] | None:
        self.cursor.execute(
            "select id, kind, name, aliases, description, status, possible_match_of "
            "from public.entities where id = %s",
            (str(entity_id),),
        )
        row = self.cursor.fetchone()
        return dict(row) if row else None


def parse(text: str, schema: Schema) -> tuple[list[dict[str, str]], list[dict[str, str]]] | None:
    """Things and links from the model's reply, kept to the schema. None when
    the reply cannot be read at all."""
    found = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not found:
        return None
    try:
        data = json.loads(found.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    things: list[dict[str, str]] = []
    seen: set[str] = set()
    for t in data.get("things") or []:
        if not isinstance(t, dict):
            continue
        name = " ".join(str(t.get("name") or "").split())[:200]
        kind = str(t.get("kind") or "").strip().lower()
        if not name or kind not in schema.kinds or name.lower() in seen:
            continue
        seen.add(name.lower())
        things.append(
            {
                "name": name,
                "kind": kind,
                "description": " ".join(str(t.get("description") or "").split()),
            }
        )
    things = things[:MAX_THINGS]
    names = {t["name"].lower() for t in things}
    links: list[dict[str, str]] = []
    for link in data.get("links") or []:
        if not isinstance(link, dict):
            continue
        a, rel, b = (" ".join(str(link.get(k) or "").split()) for k in ("from", "relation", "to"))
        rel = rel.lower()
        if (
            rel in schema.relations
            and a.lower() in names
            and b.lower() in names
            and a.lower() != b.lower()
        ):
            links.append({"from": a, "relation": rel, "to": b})
    return things, links[:MAX_LINKS]


def _schema(cursor: psycopg.Cursor, org_id: UUID) -> Schema:
    cursor.execute(
        "select value from public.system_flags where org_id = %s and key = %s",
        (str(org_id), SCHEMA_FLAG),
    )
    row = cursor.fetchone()
    value = row["value"] if row and isinstance(row["value"], dict) else {}
    return Schema(
        kinds=tuple(value.get("kinds") or DEFAULT_SCHEMA.kinds),
        relations=tuple(value.get("relations") or DEFAULT_SCHEMA.relations),
    )


def _describe(entity: dict[str, Any]) -> str:
    also = f"; also called {', '.join(entity['aliases'])}" if entity.get("aliases") else ""
    what = f": {entity['description']}" if entity.get("description") else ""
    return f"{entity['name']} ({entity['kind']}{also}){what}"[:500]


def _name_neighbourhoods(session: "Session", run: "_Run", cursor: psycopg.Cursor) -> int:
    """Name a few of the map's neighbourhoods from their claims. A model error
    stops the naming, never the run: the names wait for the next run."""
    cursor.execute(
        "select body from public.agent_prompts where agent_id = %s and slot = %s and active",
        (str(run.agent_id), NAME_SLOT),
    )
    prompt = cursor.fetchone()
    if prompt is None:
        return 0
    conf = layout.settings(cursor, run.org_id)
    limit, max_tokens = int(conf["names_per_run"]), int(conf["name_max_tokens"])
    named = 0
    for group in layout.unnamed(cursor, run.org_id, limit):
        if not group["claims"]:
            continue
        try:
            response = session.gateway.complete(
                agent_id=run.agent_id,
                run_id=run.id,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": prompt["body"]},
                    {
                        "role": "user",
                        "content": json.dumps({"facts": group["claims"]}, ensure_ascii=False),
                    },
                ],
            )
        except GatewayError:
            break
        if layout.name(cursor, group["id"], response.text):
            named += 1
    return named


def _result(
    status: str, reason: str, *, output: dict[str, Any] | None = None, error: str | None = None
) -> dict[str, Any]:
    return {"status": status, "reason": reason, "output": output, "error": error}


# --- Setting up the librarian (owner's act) -------------------------------------

DEPARTMENT = "brain"
AGENT = "librarian"
ROUTINE = "Brain librarian"


def ensure_librarian(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    time_of_day: str = "07:40",
    timezone: str = "America/New_York",
    budget_usd: str = "0.25",
) -> dict[str, Any]:
    """The librarian's department, agent, prompt and daily routine, made once.

    The routine is created switched off, like every new trigger: the owner
    switches it on (`scripts.graph on`). Running again changes nothing.
    """
    from app.agents.starter_prompts import STARTER_PROMPTS
    from app.db import acting_as

    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into public.departments (org_id, name, daily_budget_usd) values (%s, %s, %s) "
            "on conflict (org_id, name) do nothing",
            (str(org_id), DEPARTMENT, budget_usd),
        )
        cursor.execute(
            "select id from public.departments where org_id = %s and name = %s",
            (str(org_id), DEPARTMENT),
        )
        department_id = cursor.fetchone()["id"]
        cursor.execute(
            "select id from public.agents where org_id = %s and name = %s", (str(org_id), AGENT)
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "insert into public.agents (org_id, department_id, name, role, model_tier, "
                "runner, enabled) values (%s, %s, %s, 'brain', 'cheap', 'librarian', true) "
                "returning id",
                (str(org_id), department_id, AGENT),
            )
            row = cursor.fetchone()
        agent_id = row["id"]
        for slot in (PROMPT_SLOT, NAME_SLOT):
            cursor.execute(
                "insert into public.agent_prompts (org_id, agent_id, slot, version, body, "
                "note, active) select %s, %s, %s, 1, %s, 'Starting prompt', true where not "
                "exists (select 1 from public.agent_prompts where agent_id = %s and slot = %s)",
                (str(org_id), agent_id, slot, STARTER_PROMPTS["librarian"][slot], agent_id, slot),
            )
        cursor.execute(
            """
            insert into public.triggers
                (org_id, agent_id, name, task, time_of_day, days_of_week, timezone, run_as,
                 routine_key, max_steps, max_tokens)
            values (%s, %s, %s, %s, %s, '{0,1,2,3,4,5,6}', %s, %s, 'brain:librarian', 5, 100000)
            on conflict (org_id, name) do nothing
            """,
            (
                str(org_id),
                agent_id,
                ROUTINE,
                json.dumps({"kind": "graph"}),
                time_of_day,
                timezone,
                str(user_id),
            ),
        )
        cursor.execute(
            "select id, enabled from public.triggers where org_id = %s and name = %s",
            (str(org_id), ROUTINE),
        )
        trigger = cursor.fetchone()
    from app.db import as_service_role

    with as_service_role(connection) as conn:
        conn.execute(
            "insert into public.system_flags (org_id, key, value) values (%s, %s, %s) "
            "on conflict (org_id, key) do nothing",
            (
                str(org_id),
                SCHEMA_FLAG,
                json.dumps(
                    {
                        "kinds": list(DEFAULT_SCHEMA.kinds),
                        "relations": list(DEFAULT_SCHEMA.relations),
                    }
                ),
            ),
        )
    return {"agent_id": agent_id, "trigger_id": trigger["id"], "enabled": trigger["enabled"]}
