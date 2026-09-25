"""Step 8.0: department charters as data, applied to agents and routines (ADR 024)."""

from typing import Any
from uuid import UUID

import psycopg
import pytest
from pydantic import ValidationError

from app.brain import Brain, HashingEmbedder
from app.brain.write_gate import BrainWriter, FactCandidate
from app.db import acting_as, as_service_role
from app.departments.apply import apply_charter, enable
from app.departments.charter import Charter, CharterError, load, publish, seed_charters
from app.departments.starter_charters import RESEARCH, STARTER_CHARTERS
from app.gateway import Gateway
from app.judge import Judge
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from app.tools import ToolContext, ToolRuntime
from tests.conftest_db import Tenants
from tests.scripted_jev import ScriptedJev
from tests.test_approvals import Explainer
from tests.test_gateway import TIERS
from tests.test_judge import set_price


@pytest.fixture
def org(db: psycopg.Connection, tenants: Tenants) -> Tenants:
    set_price(db, tenants.org_a)
    seed_gates(db, user_id=tenants.user_a, org_id=tenants.org_a, gates=STARTER_GATES)
    seed_charters(db, user_id=tenants.user_a, org_id=tenants.org_a, charters=STARTER_CHARTERS)
    return tenants


def agents(db: psycopg.Connection, org_id: UUID) -> dict[str, dict[str, Any]]:
    with as_service_role(db) as conn:
        rows = conn.execute(
            "select a.name, a.role_type, a.runner, a.enabled, a.allowed_tools, a.autonomy_level, "
            "p.name as parent from public.agents a "
            "left join public.agents p on p.id = a.parent_agent_id where a.org_id = %s",
            (str(org_id),),
        ).fetchall()
    return {r["name"]: r for r in rows}


def triggers(db: psycopg.Connection, org_id: UUID) -> list[dict[str, Any]]:
    with as_service_role(db) as conn:
        return conn.execute(
            "select name, routine_key, enabled, time_of_day, task from public.triggers "
            "where org_id = %s",
            (str(org_id),),
        ).fetchall()


# --- The charter itself --------------------------------------------------------------


def test_the_starting_charters_are_valid_and_two_are_drafts() -> None:
    assert not RESEARCH.draft
    assert STARTER_CHARTERS["executive"].draft and STARTER_CHARTERS["marketing"].draft
    assert [a.name for a in RESEARCH.agents] == ["research-lead", "web-researcher", "fact-curator"]


def test_a_routine_for_an_agent_outside_the_charter_is_refused() -> None:
    data = RESEARCH.model_dump(mode="json")
    data["routine"][0]["agent"] = "someone-else"
    with pytest.raises(ValidationError, match="not in this charter"):
        Charter.model_validate(data)


def test_charters_are_versioned_audited_and_frozen(db: psycopg.Connection, org: Tenants) -> None:
    changed = RESEARCH.model_copy(update={"daily_budget_usd": RESEARCH.daily_budget_usd * 2})
    version = publish(
        db, user_id=org.user_a, org_id=org.org_a, department="research", charter=changed
    )

    assert version == 2
    live_version, live = load(db, org_id=org.org_a, department="research")
    assert (live_version, live.daily_budget_usd) == (2, changed.daily_budget_usd)
    with as_service_role(db) as conn:
        events = conn.execute(
            "select payload->>'department' as d, payload->>'version' as v from public.events "
            "where type = 'charter_activated' order by created_at, v"
        ).fetchall()
    assert ("research", "2") in {(e["d"], e["v"]) for e in events}
    # Only the live flag may change: the column grant refuses anything else,
    # and the freeze trigger backs it up for the table's owner.
    with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.RestrictViolation)):
        with as_service_role(db) as conn:
            conn.execute("update public.department_charters set note = 'x' where version = 1")


def test_an_agent_cannot_write_a_charter(db: psycopg.Connection, org: Tenants) -> None:
    from tests.test_gateway import make_agent

    agent = make_agent(db, org.org_a, name="sneaky")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with acting_as(db, user_id=str(org.user_a), agent_id=str(agent)) as conn:
            conn.execute(
                "select public.publish_department_charter(%s, 'research', '{}'::jsonb)",
                (str(org.org_a),),
            )


# --- Applying it -------------------------------------------------------------------------


def test_applying_creates_the_department_switched_off(db: psycopg.Connection, org: Tenants) -> None:
    report = apply_charter(db, user_id=org.user_a, org_id=org.org_a, department="research")

    assert "department research" in report.created
    made = agents(db, org.org_a)
    assert set(made) == {"research-lead", "web-researcher", "fact-curator"}
    assert made["research-lead"]["role_type"] == "head"
    assert made["web-researcher"]["parent"] == "research-lead"
    assert not any(a["enabled"] for a in made.values()), "nothing runs before the owner says so"
    (trigger,) = triggers(db, org.org_a)
    assert trigger["routine_key"] == "research:morning-brief" and not trigger["enabled"]
    assert trigger["task"]["sources"] == []

    again = apply_charter(db, user_id=org.user_a, org_id=org.org_a, department="research")
    assert again.created == [] and again.updated == [], "applying twice changes nothing"


def test_a_new_version_updates_agents_prompts_and_routine(
    db: psycopg.Connection, org: Tenants
) -> None:
    apply_charter(db, user_id=org.user_a, org_id=org.org_a, department="research")
    enable(db, user_id=org.user_a, org_id=org.org_a, department="research")
    worker = RESEARCH.workers[0]
    changed = RESEARCH.model_copy(
        update={
            "workers": [
                worker.model_copy(
                    update={
                        "allowed_tools": ["web_fetch_preview", "report_result"],
                        "prompts": {**worker.prompts, "system": "Read pages. Report."},
                    }
                ),
                *RESEARCH.workers[1:],
            ],
            "routine": [
                RESEARCH.routine[0].model_copy(
                    update={"time": "07:15", "input": {"topics": ["Acme"], "sources": ["u"]}}
                )
            ],
        }
    )
    publish(db, user_id=org.user_a, org_id=org.org_a, department="research", charter=changed)

    report = apply_charter(db, user_id=org.user_a, org_id=org.org_a, department="research")

    assert any(
        u.startswith("agent web-researcher (allowed_tools, prompt system)") for u in report.updated
    ), report.updated
    assert any(u.startswith("trigger Morning research brief") for u in report.updated)
    assert agents(db, org.org_a)["web-researcher"]["allowed_tools"] == [
        "web_fetch_preview",
        "report_result",
    ]
    (trigger,) = triggers(db, org.org_a)
    assert str(trigger["time_of_day"]) == "07:15:00" and trigger["enabled"], (
        "stays as the owner set it"
    )
    assert trigger["task"]["sources"] == ["u"]
    with as_service_role(db) as conn:
        versions = conn.execute(
            "select version from public.agent_prompts p join public.agents a on a.id = p.agent_id "
            "where a.name = 'web-researcher' and p.slot = 'system' order by version"
        ).fetchall()
    assert [v["version"] for v in versions] == [1, 2]

    # A routine item that leaves the charter is switched off, not deleted.
    publish(
        db,
        user_id=org.user_a,
        org_id=org.org_a,
        department="research",
        charter=changed.model_copy(update={"routine": []}),
    )
    gone = apply_charter(db, user_id=org.user_a, org_id=org.org_a, department="research")
    assert gone.switched_off == ["trigger Morning research brief"]
    assert not triggers(db, org.org_a)[0]["enabled"]


def test_a_draft_or_a_charter_needing_unbuilt_tools_is_not_applied(
    db: psycopg.Connection, org: Tenants
) -> None:
    with pytest.raises(CharterError, match="draft"):
        apply_charter(db, user_id=org.user_a, org_id=org.org_a, department="marketing")
    final = STARTER_CHARTERS["marketing"].model_copy(update={"draft": False})
    publish(db, user_id=org.user_a, org_id=org.org_a, department="marketing", charter=final)
    with pytest.raises(CharterError, match="save_draft"):
        apply_charter(db, user_id=org.user_a, org_id=org.org_a, department="marketing")


def test_enable_switches_the_department_on_and_off_together(
    db: psycopg.Connection, org: Tenants
) -> None:
    apply_charter(db, user_id=org.user_a, org_id=org.org_a, department="research")

    on = enable(db, user_id=org.user_a, org_id=org.org_a, department="research")
    off = enable(db, user_id=org.user_a, org_id=org.org_a, department="research", on=False)

    assert len(on) == 4 and len(off) == 4
    assert not any(a["enabled"] for a in agents(db, org.org_a).values())


# --- Brain hygiene ---------------------------------------------------------------------------


def test_the_hygiene_scan_lists_stale_and_disputed_facts(
    db: psycopg.Connection, org: Tenants
) -> None:
    apply_charter(db, user_id=org.user_a, org_id=org.org_a, department="research")
    enable(db, user_id=org.user_a, org_id=org.org_a, department="research")
    curator = agents(db, org.org_a)
    with as_service_role(db) as conn:
        curator_id = conn.execute(
            "select id from public.agents where name = 'fact-curator' and org_id = %s",
            (str(org.org_a),),
        ).fetchone()["id"]
    assert curator
    claims = [
        "Acme Corp has 40 employees.",
        "Acme Corp was founded in 2019.",
        "Acme Corp is based in Leeds.",
    ]
    with acting_as(db, user_id=str(org.user_a)) as conn:
        gateway = Gateway(conn, Explainer(), TIERS, systemone=ScriptedJev())
        writer = BrainWriter(conn, Brain(conn, HashingEmbedder()), Judge(conn, gateway))
        ids = [
            writer.propose(
                FactCandidate(claim=c, source="owner", source_text=c),
                org_id=org.org_a,
                agent_id=curator_id,
            ).fact.id
            for c in claims
        ]
    with as_service_role(db) as conn:
        conn.execute(
            "update public.facts set review_after = current_date - 1 where id = %s", (str(ids[0]),)
        )
        conn.execute("update public.facts set status = 'disputed' where id = %s", (str(ids[1]),))

    with acting_as(db, user_id=str(org.user_a), agent_id=str(curator_id)) as conn:
        result = ToolRuntime(
            ToolContext(
                connection=conn, org_id=org.org_a, agent_id=curator_id, agent_name="fact-curator"
            )
        ).call("brain_hygiene_scan", {})

    assert result.status == "ok", result.output
    assert [(f["claim"], f["why"]) for f in result.output["needs_a_look"]] == [
        ("Acme Corp was founded in 2019.", "disputed"),
        ("Acme Corp has 40 employees.", "past its review date"),
    ]
    assert result.output["totals"] == {"active": 2, "disputed": 1, "superseded": 0}
