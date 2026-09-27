"""The brain's graph: things, links, matching and tidying (ADR 035)."""

import dataclasses
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.agents.librarian import Schema, ensure_librarian, parse
from app.brain import Brain, HashingEmbedder
from app.db import acting_as, as_service_role, connect
from app.gateway import ModelResponse
from app.tasks import order
from tests.conftest_db import admit
from tests.scripted_jev import ScriptedJev, choice
from tests.test_chief_of_staff import Office, office, run_next, runtime  # noqa: F401

SCHEMA = Schema(
    kinds=("person", "company", "product", "project", "investor", "fund", "topic"),
    relations=("founded", "works_on", "invested_in", "competes_with", "part_of"),
)

#: What the librarian's model reads out of each fact, as a person would.
READS: dict[str, dict[str, Any]] = {
    "Priya Rao founded Lumen, which audits AI agents.": {
        "things": [
            {"name": "Priya Rao", "kind": "person", "description": "Founder of Lumen."},
            {"name": "Lumen", "kind": "company", "description": "Audits AI agents."},
        ],
        "links": [{"from": "Priya Rao", "relation": "founded", "to": "Lumen"}],
    },
    "Gradient Ventures invested in Lumen's $4M seed round.": {
        "things": [
            {"name": "Gradient Ventures", "kind": "investor", "description": "A VC firm."},
            {"name": "Lumen", "kind": "company", "description": "Raised a $4M seed."},
        ],
        "links": [{"from": "Gradient Ventures", "relation": "invested_in", "to": "Lumen"}],
    },
    "Think is a Saudi AI startup founded by Ahmed AlSharif.": {
        "things": [
            {"name": "Think", "kind": "company", "description": "A Saudi AI startup."},
            {"name": "Ahmed AlSharif", "kind": "person", "description": "Founder of Think."},
        ],
        "links": [{"from": "Ahmed AlSharif", "relation": "founded", "to": "Think"}],
    },
    "Think, a US robotics firm, makes warehouse arms.": {
        "things": [{"name": "Think", "kind": "company", "description": "A US robotics firm."}],
        "links": [],
    },
    "Lumen and Tessel both build tools for AI agents.": {
        "things": [
            {"name": "Lumen", "kind": "company", "description": "Builds tools for agents."},
            {"name": "Tessel", "kind": "company", "description": "Builds tools for agents."},
        ],
        "links": [{"from": "Lumen", "relation": "competes_with", "to": "Tessel"}],
    },
    "Lumen Labs Inc. opened an office in London.": {
        "things": [{"name": "Lumen Labs Inc.", "kind": "company", "description": "Lumen."}],
        "links": [],
    },
}


@dataclass
class Reader:
    """The librarian's model, scripted."""

    calls: list[str] = field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        fact = json.loads(messages[-1]["content"])["fact"]
        self.calls.append(fact)
        text = "I can't read that." if "unreadable" in fact else json.dumps(READS.get(fact, {}))
        return ModelResponse(
            model=model,
            text=text,
            tokens_in=200,
            tokens_out=80,
            cost_usd=0.0001,
            latency_ms=1,
            provider="scripted",
        )


def judge(match: dict[str, tuple[str, float]], unsupported: set[str] = frozenset()) -> ScriptedJev:  # type: ignore[assignment]
    """Jev, scripted: `match` maps a mention's context to (which candidate
    name, probability); a link is supported unless its text is listed."""

    def respond(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        if "match" in questions:
            options = list(questions["match"].criteria)
            name, p = match.get(state["context"], ("new", 0.95))
            picked = next(
                (k for k in options if k != "new" and f"c{k[1:]}: {name} (" in state["candidates"]),
                "new",
            )
            return {"match": choice(picked, options, p)}
        if "states" in questions:
            from tests.scripted_jev import noul

            return {"states": noul(0.05 if state["link"] in unsupported else 0.95)}
        return {}

    return ScriptedJev(respond=respond)


def add_facts(dsn: str, where: Office, *claims: str) -> None:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        brain = Brain(conn, HashingEmbedder())
        for claim in claims:
            brain.insert_fact(
                org_id=where.org_id,
                claim=claim,
                admission=dataclasses.replace(admit(conn, where.org_id)),
            )


def read_now(dsn: str, where: Office, jev: ScriptedJev, reader: Reader) -> Any:
    with connect(dsn) as connection:
        ensure_librarian(connection, user_id=where.user_id, org_id=where.org_id)
        order(
            connection,
            user_id=where.user_id,
            org_id=where.org_id,
            agent="librarian",
            title="Read new facts into the graph",
            idempotency_key=str(uuid.uuid4()),
        )
    report = run_next(dsn, runtime(dsn, jev, reader))
    assert report.status == "succeeded", report.error
    return report.output


def things(dsn: str, where: Office) -> dict[str, list[dict[str, Any]]]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        rows = conn.execute(
            "select id, kind, name, aliases, status, merged_into, possible_match_of "
            "from public.entities where org_id = %s order by created_at",
            (str(where.org_id),),
        ).fetchall()
    found: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        found.setdefault(r["name"], []).append(dict(r))
    return found


def links(dsn: str, where: Office) -> set[tuple[str, str, str]]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return {
            (r["a"], r["relation"], r["b"])
            for r in conn.execute(
                "select a.name as a, l.relation, b.name as b from public.entity_links l "
                "join public.entities a on a.id = l.from_entity "
                "join public.entities b on b.id = l.to_entity "
                "where l.org_id = %s and l.status = 'active'",
                (str(where.org_id),),
            )
        }


def test_a_fact_that_arrives_later_joins_the_things_already_there(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    add_facts(dsn, office, "Priya Rao founded Lumen, which audits AI agents.")
    jev = judge({"Gradient Ventures invested in Lumen's $4M seed round.": ("Lumen", 0.95)})
    first = read_now(dsn, office, jev, Reader())
    assert (first["things_new"], first["links"]) == (2, 1)

    # Days later, a new fact about Lumen: it attaches to the same Lumen.
    add_facts(dsn, office, "Gradient Ventures invested in Lumen's $4M seed round.")
    second = read_now(dsn, office, jev, Reader())

    assert (second["things_new"], second["things_joined"], second["links"]) == (1, 1, 1)
    found = things(dsn, office)
    assert len(found["Lumen"]) == 1, "one Lumen, not two islands"
    assert links(dsn, office) == {
        ("Priya Rao", "founded", "Lumen"),
        ("Gradient Ventures", "invested_in", "Lumen"),
    }
    with connect(dsn) as connection, as_service_role(connection) as conn:
        mentions = conn.execute(
            "select count(*) as n from public.fact_entities where entity_id = %s",
            (str(found["Lumen"][0]["id"]),),
        ).fetchone()["n"]
        states = {
            r["graph_state"]
            for r in conn.execute(
                "select graph_state from public.facts where org_id = %s", (str(office.org_id),)
            )
        }
    assert mentions == 2 and states == {"done"}
    assert first["unlinked_things"] == 0 and second["still_pending"] == 0


def test_two_companies_called_think_stay_apart(dsn: str, office: Office) -> None:  # noqa: F811
    add_facts(dsn, office, "Think is a Saudi AI startup founded by Ahmed AlSharif.")
    read_now(dsn, office, judge({}), Reader())
    add_facts(dsn, office, "Think, a US robotics firm, makes warehouse arms.")

    # Jev sees both descriptions and says: a different thing.
    jev = judge({"Think, a US robotics firm, makes warehouse arms.": ("new", 0.95)})
    output = read_now(dsn, office, jev, Reader())

    assert output["things_new"] == 1
    thinks = things(dsn, office)["Think"]
    assert len(thinks) == 2 and all(t["possible_match_of"] is None for t in thinks)
    assert len(jev.calls_for("match")) >= 1, "Jev was asked which Think this is"


def test_a_link_the_fact_does_not_state_is_not_kept(dsn: str, office: Office) -> None:  # noqa: F811
    add_facts(dsn, office, "Lumen and Tessel both build tools for AI agents.")

    read_now(dsn, office, judge({}, unsupported={"Lumen competes with Tessel"}), Reader())

    assert links(dsn, office) == set()
    assert set(things(dsn, office)) == {"Lumen", "Tessel"}


def test_an_unsure_match_is_kept_apart_then_merged_when_jev_is_sure(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    add_facts(dsn, office, "Priya Rao founded Lumen, which audits AI agents.")
    read_now(dsn, office, judge({}), Reader())
    add_facts(dsn, office, "Lumen Labs Inc. opened an office in London.")
    # Reading the new fact, Jev is unsure (0.55): a new thing, marked.
    # The tidy-up in the same run asks again (context "Lumen."): still unsure.
    unsure = judge(
        {"Lumen Labs Inc. opened an office in London.": ("Lumen", 0.55), "Lumen.": ("Lumen", 0.55)}
    )
    reader = Reader()
    with connect(dsn) as connection, as_service_role(connection) as conn:
        # Near by meaning is what makes Lumen a candidate for "Lumen Labs Inc.".
        conn.execute(
            "update public.entities set aliases = '{Lumen Labs Inc.}' where name = 'Lumen'"
        )
    output = read_now(dsn, office, unsure, reader)
    assert output["things_new"] == 1

    found = things(dsn, office)
    # The alias made the match: with 0.55 it is not joined, it is marked,
    # and the unsure tidy-up leaves the mark for next time.
    labs = found["Lumen Labs Inc."]
    assert labs[0]["status"] == "active"
    assert labs[0]["possible_match_of"] == found["Lumen"][0]["id"]

    # The tidy-up asks again; now Jev is sure, and the two become one.
    sure = judge({"Lumen.": ("Lumen", 0.95), "Lumen Labs Inc.": ("Lumen", 0.95)})
    read_now(dsn, office, sure, Reader())

    found = things(dsn, office)
    active = [
        t
        for rows in found.values()
        for t in rows
        if t["status"] == "active" and "Lumen" in t["name"]
    ]
    assert len(active) == 1 and active[0]["name"] == "Lumen"
    assert "Lumen Labs Inc." in active[0]["aliases"]
    merged = [t for t in found.get("Lumen Labs Inc.", []) if t["status"] == "merged"]
    assert all(t["merged_into"] == active[0]["id"] for t in merged)


def test_an_unreadable_reply_skips_the_fact_once(dsn: str, office: Office) -> None:  # noqa: F811
    add_facts(dsn, office, "An unreadable fact.")
    read_now(dsn, office, judge({}), Reader())
    reader = Reader()
    read_now(dsn, office, judge({}), reader)

    assert reader.calls == [], "a skipped fact is not read again"
    with connect(dsn) as connection, as_service_role(connection) as conn:
        state = conn.execute(
            "select graph_state from public.facts where org_id = %s", (str(office.org_id),)
        ).fetchone()["graph_state"]
    assert state == "skipped"


def test_setup_is_idempotent_and_the_routine_starts_off(dsn: str, office: Office) -> None:  # noqa: F811
    with connect(dsn) as connection:
        first = ensure_librarian(connection, user_id=office.user_id, org_id=office.org_id)
        again = ensure_librarian(connection, user_id=office.user_id, org_id=office.org_id)
        with acting_as(connection, user_id=str(office.user_id)) as conn:
            runner = conn.execute(
                "select runner from public.agents where id = %s", (str(first["agent_id"]),)
            ).fetchone()["runner"]
    assert first == again and first["enabled"] is False and runner == "librarian"


def test_the_model_is_kept_to_the_schema() -> None:
    reply = json.dumps(
        {
            "things": [
                {"name": "Lumen", "kind": "company"},
                {"name": "lumen", "kind": "company"},
                {"name": "AI", "kind": "buzzword"},
                {"name": "Priya Rao", "kind": "person"},
            ],
            "links": [
                {"from": "Priya Rao", "relation": "founded", "to": "Lumen"},
                {"from": "Priya Rao", "relation": "admires", "to": "Lumen"},
                {"from": "Priya Rao", "relation": "founded", "to": "Nobody"},
                {"from": "Lumen", "relation": "part_of", "to": "Lumen"},
            ],
        }
    )
    found = parse(f"Here you go:\n{reply}", SCHEMA)
    assert found is not None
    kept_things, kept_links = found
    assert [t["name"] for t in kept_things] == ["Lumen", "Priya Rao"]
    assert kept_links == [{"from": "Priya Rao", "relation": "founded", "to": "Lumen"}]
    assert parse("no json here", SCHEMA) is None
