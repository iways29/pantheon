"""What may enter the brain (ADR 033): it grows with use, not with the web."""

import json
import uuid
from typing import Any
from uuid import UUID

import psycopg

from app.brain import Brain, HashingEmbedder, memory
from app.brain.policy import BrainPolicy, load
from app.brain.write_gate import BrainWriter
from app.db import acting_as, as_service_role, connect
from app.gateway import Gateway
from app.judge import Judge
from app.tools import ToolContext, ToolRuntime, seed_tools
from tests.conftest_db import Tenants, allow_agent_facts
from tests.scripted_jev import ScriptedJev, choice
from tests.test_chief_of_staff import Office, Writer, office, run_next, runtime  # noqa: F401
from tests.test_links import CLAIMS, ClaimsModel, agent, detector, facts, links, page  # noqa: F401
from tests.test_runners import TIERS


def tools(conn: psycopg.Connection, tenants: Tenants, agent_id: UUID, jev: ScriptedJev) -> Any:
    gateway = Gateway(conn, ClaimsModel(), TIERS, systemone=jev)
    judge = Judge(conn, gateway)
    brain = Brain(conn, HashingEmbedder())
    return ToolRuntime(
        ToolContext(
            connection=conn,
            org_id=tenants.org_a,
            agent_id=agent_id,
            agent_name="researcher",
            gateway=gateway,
            brain=brain,
            writer=BrainWriter(conn, brain, judge),
            judge=judge,
            links=links(conn, ClaimsModel(), jev),
        )
    )


def allow(db: psycopg.Connection, agent_id: UUID, *names: str) -> None:
    with as_service_role(db) as conn:
        conn.execute(
            "update public.agents set allowed_tools = %s where id = %s",
            (list(names), str(agent_id)),
        )


def test_the_default_keeps_agents_web_reads_and_claims_out_of_the_brain(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: UUID,  # noqa: F811
) -> None:
    seed_tools(db, user_id=tenants.user_a, org_id=tenants.org_a)
    allow(db, agent, "web_push_preview", "brain_propose_fact")
    jev = ScriptedJev(respond=detector)
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        assert load(conn, tenants.org_a) == BrainPolicy()
        preview = links(conn, ClaimsModel(), jev).preview(
            page(), org_id=tenants.org_a, agent_id=agent
        )
        rt = tools(conn, tenants, agent, jev)
        pushed = rt.call("web_push_preview", {"preview_id": str(preview.id)})
        claimed = rt.call(
            "brain_propose_fact",
            {
                "claim": "Acme Corp was founded in 2019.",
                "evidence": "Acme Corp was founded in 2019.",
            },
        )

    assert pushed.status == "ok", pushed.output
    assert [r["outcome"] for r in pushed.output["results"]] == ["noted", "noted"]
    assert "today's findings" in pushed.output["note"]
    assert claimed.output["outcome"] == "not_kept"
    assert facts(db, tenants.org_a) == []
    assert not jev.calls_for("support"), "nothing was judged: nothing was going to be kept"
    with as_service_role(db) as conn:
        row = conn.execute(
            "select status, claims from public.link_previews where id = %s", (str(preview.id),)
        ).fetchone()
    assert row["status"] == "pushed" and row["claims"] == CLAIMS, "the findings stay for the day"


def test_the_policy_is_data_and_turning_it_on_restores_the_old_behaviour(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: UUID,  # noqa: F811
) -> None:
    seed_tools(db, user_id=tenants.user_a, org_id=tenants.org_a)
    allow(db, agent, "web_push_preview")
    allow_agent_facts(db, tenants.org_a)
    jev = ScriptedJev(respond=detector)
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        preview = links(conn, ClaimsModel(), jev).preview(
            page(), org_id=tenants.org_a, agent_id=agent
        )
        pushed = tools(conn, tenants, agent, jev).call(
            "web_push_preview", {"preview_id": str(preview.id)}
        )

    assert [r["outcome"] for r in pushed.output["results"]] == ["accepted", "accepted"]
    assert len(facts(db, tenants.org_a)) == 2
    with as_service_role(db) as conn:
        changed = conn.execute(
            "select payload from public.events where org_id = %s and type = 'brain_policy_changed'",
            (str(tenants.org_a),),
        ).fetchone()
    assert changed["payload"]["value"]["agent_web_facts"] is True


def test_the_owners_own_link_push_is_still_kept(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: UUID,  # noqa: F811
) -> None:
    jev = ScriptedJev(respond=detector)
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        ls = links(conn, ClaimsModel(), jev)
        preview = ls.preview(page(), org_id=tenants.org_a, agent_id=agent)
        pushed = ls.push(preview.id, org_id=tenants.org_a)

    assert [r["outcome"] for r in pushed.results] == ["accepted", "accepted"]
    assert len(facts(db, tenants.org_a)) == 2


def owner_facts(dsn: str, org_id: uuid.UUID) -> list[str]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return [
            r["claim"]
            for r in conn.execute(
                "select claim from public.facts where org_id = %s and source = 'owner' "
                "order by claim",
                (str(org_id),),
            )
        ]


def sorter() -> ScriptedJev:
    """Sorts the owner's words as a person would (memory_triage)."""
    kinds = {"boss": "preference", "legal": "interest", "Tessel": "interest", "thanks": "forget"}

    def respond(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        if "kind" not in questions:
            return {}
        said = state["said"]
        kind = next((k for word, k in kinds.items() if word in said), "fact")
        return {"kind": choice(kind, list(questions["kind"].criteria))}

    return ScriptedJev(respond=respond)


def brief(
    dsn: str, where: Office, jev: ScriptedJev | None = None, writer: Any = None
) -> dict[str, Any]:
    from app.tasks import order

    with connect(dsn) as connection, as_service_role(connection) as conn:
        trigger = conn.execute(
            "select id from public.triggers where org_id = %s and routine_key = "
            "'executive:morning-brief'",
            (str(where.org_id),),
        ).fetchone()
    with connect(dsn) as connection:
        task = order(
            connection,
            user_id=where.user_id,
            org_id=where.org_id,
            agent="brief-writer",
            title="Morning brief",
            idempotency_key=str(uuid.uuid4()),
        )
        # As the schedule makes it: a trigger's task, not an order.
        with as_service_role(connection) as conn:
            conn.execute(
                "update public.tasks set created_by = %s where id = %s",
                (f"trigger:{trigger['id'] if trigger else 'x'}", str(task.id)),
            )
    report = run_next(dsn, runtime(dsn, jev or ScriptedJev(), writer or Writer()))
    assert report.status == "succeeded", report.error
    return report.output or {}


def test_each_morning_the_brain_learns_what_the_owner_said_and_did(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    from app.brain.memory import preferences
    from app.tasks import order

    with connect(dsn) as connection:
        for title, text in (
            ("Legal AI", "Who is building AI for legal teams in India?"),
            ("How to address me", "From now on, call me boss."),
            ("Thanks", "thanks, run it again"),
        ):
            ordered = order(
                connection,
                user_id=office.user_id,
                org_id=office.org_id,
                agent="research-lead",
                title=title,
                instructions=text,
            )
            with as_service_role(connection) as conn:
                # Done already: only the brief is left for the scheduler.
                conn.execute(
                    "update public.tasks set status = 'done', finished_at = now() where id = %s",
                    (str(ordered.id),),
                )
        with as_service_role(connection) as conn:
            conn.execute(
                "insert into public.routine_requests (org_id, routine_key, request) "
                "values (%s, 'research:morning-brief', 'Which funds backed Tessel?')",
                (str(office.org_id),),
            )
            agent_id = conn.execute(
                "select id from public.agents where org_id = %s and name = 'brief-writer'",
                (str(office.org_id),),
            ).fetchone()["id"]
            conn.execute(
                "insert into public.approvals (org_id, agent_id, action_type, payload, status, "
                "decided_at) values (%s, %s, 'draft_review', %s, 'approved', now())",
                (
                    str(office.org_id),
                    str(agent_id),
                    json.dumps({"channel": "x", "title": "The first fort"}),
                ),
            )

    output = brief(dsn, office, sorter())

    assert output["remembered"] == {
        "accepted:interest": 2,
        "accepted:preference": 1,
        "accepted": 1,
        "forgotten": 1,
    }
    learned = owner_facts(dsn, office.org_id)
    assert len(learned) == 4, learned
    assert any("the owner asked about: Who is building AI for legal teams" in f for f in learned)
    assert any("asked about: Which funds backed Tessel?" in f for f in learned)
    assert any("approved a x draft titled: The first fort" in f for f in learned)
    assert not any("run it again" in f for f in learned), "thanks is forgotten, never asked"
    with connect(dsn) as connection, as_service_role(connection) as conn:
        (boss,) = preferences(conn, office.org_id)
        kinds = {
            r["kind"]
            for r in conn.execute(
                "select kind from public.facts where org_id = %s", (str(office.org_id),)
            )
        }
    assert "call me boss" in boss and boss.startswith("The owner's preference (from ")
    assert kinds == {"interest", "preference", "fact"}

    # The next morning: the brief follows the preference; nothing is kept twice.
    writer = Writer()
    assert brief(dsn, office, sorter(), writer)["remembered"] == {}
    system = writer.calls[0][0]["content"]
    assert "standing preferences" in system and "call me boss" in system
    assert len(owner_facts(dsn, office.org_id)) == 4


def test_an_order_to_the_chief_of_staff_is_learned_within_a_minute(
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    from app.brain.memory import preferences
    from app.owner_api import give_order

    with connect(dsn) as connection:
        give_order(connection, str(office.user_id), str(office.org_id), "Call me boss from now on.")
    run_next(dsn, runtime(dsn, sorter(), Writer()))

    with connect(dsn) as connection, as_service_role(connection) as conn:
        assert any("Call me boss" in p for p in preferences(conn, office.org_id))


def test_what_is_remembered_is_the_owners_choice() -> None:
    quiet = BrainPolicy(remember_orders=False, remember_asks=False, remember_approved_drafts=False)

    class Nothing:
        def execute(self, *a: Any, **k: Any) -> None:
            raise AssertionError("nothing should be read")

    assert memory.moments(Nothing(), org_id=uuid.uuid4(), since=None, policy=quiet) == []  # type: ignore[arg-type]
