"""The Control Center's endpoints (Step 11, ADR 039).

Committed, like test_chief_of_staff: the starter charters are applied for
research and executive, then changed only through the API, as the owner.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.control import get_catalogue
from app.db import as_service_role, connect
from app.main import app
from tests.conftest import AUDIENCE, SECRET
from tests.conftest_db import admit
from tests.test_chief_of_staff import Office, office  # noqa: F401  (a fixture)
from tests.test_health import auth, make_token

CATALOGUE = [
    {"id": "openai/gpt-6-luna", "name": "Luna", "in_usd_per_mtok": 0.1, "out_usd_per_mtok": 0.4},
    {"id": "openai/gpt-5.6-sol", "name": "Sol", "in_usd_per_mtok": 1.0, "out_usd_per_mtok": 4.0},
]


@pytest.fixture
def cc(dsn: str, office: Office) -> Iterator[TestClient]:  # noqa: F811
    settings = Settings(
        environment="test",
        database_url=dsn,
        supabase_jwt_secret=SECRET,
        supabase_jwt_audience=AUDIENCE,
        owner_user_id=str(office.user_id),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_catalogue] = lambda: CATALOGUE
    yield TestClient(app, headers=auth(make_token(str(office.user_id))))
    app.dependency_overrides.clear()


def rows(dsn: str, sql: str, *args: Any) -> list[dict[str, Any]]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute(sql, args).fetchall()


def test_departments_list_their_charter_budget_and_head(cc: TestClient) -> None:
    body = {d["name"]: d for d in cc.get("/control/departments").json()}
    assert body["research"]["head"] == "research-lead"
    assert body["research"]["charter_version"] == 1
    assert body["research"]["enabled"] is True and body["research"]["agents"] >= 2


def test_a_budget_change_goes_through_the_charter_and_is_logged(
    cc: TestClient,
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    done = cc.post("/control/departments/research/budget", json={"daily_budget_usd": 0.75})
    assert done.status_code == 200, done.text
    assert done.json()["version"] == 2
    assert "department research budget" in done.json()["applied"]["updated"]

    (dept,) = rows(
        dsn,
        "select daily_budget_usd from public.departments where name = 'research' and org_id = %s",
        str(office.org_id),
    )
    assert float(dept["daily_budget_usd"]) == 0.75
    (event,) = rows(
        dsn,
        "select payload from public.events where org_id = %s and type = 'department_updated'",
        str(office.org_id),
    )
    assert event["payload"]["daily_budget_usd"] == 0.75
    assert str(event["payload"]["changed_by"]) == str(office.user_id)

    versions = cc.get("/control/departments/research/charters").json()
    assert [v["version"] for v in versions] == [2, 1]
    back = cc.post("/control/departments/research/restore", json={"version": 1})
    assert back.status_code == 200 and back.json()["version"] == 3


def test_a_preview_says_what_would_change_and_keeps_nothing(cc: TestClient) -> None:
    charter = cc.get("/departments/research/charter").json()["charter"]
    worker = {**charter["workers"][0], "name": "second-scout"}
    charter = {**charter, "workers": [*charter["workers"], worker]}

    preview = cc.post("/control/departments/research/preview", json={"charter": charter})
    assert preview.status_code == 200, preview.text
    assert "agent second-scout" in preview.json()["created"]
    assert [v["version"] for v in cc.get("/control/departments/research/charters").json()] == [1]
    assert "second-scout" not in {a["name"] for a in cc.get("/control/agents").json()}


def test_a_prompt_edit_is_a_version_the_charter_follows(cc: TestClient, dsn: str) -> None:
    before = [
        p for p in cc.get("/control/agents/research-lead/prompts").json() if p["slot"] == "system"
    ]
    done = cc.post(
        "/control/agents/research-lead/prompts/system",
        json={"body": "You lead research. Be brief.", "note": "shorter"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["version"] == before[0]["version"] + 1
    assert done.json()["charter_version"] == 2
    charter = cc.get("/departments/research/charter").json()["charter"]
    assert charter["head"]["prompts"]["system"] == "You lead research. Be brief."

    # Applying the charter again keeps the owner's edit.
    assert cc.post("/departments/research/apply").status_code == 200
    live = [
        p
        for p in cc.get("/control/agents/research-lead/prompts").json()
        if p["slot"] == "system" and p["active"]
    ]
    assert live[0]["body"] == "You lead research. Be brief."

    back = cc.post(
        "/control/agents/research-lead/prompts/system/activate",
        json={"version": before[0]["version"]},
    )
    assert back.status_code == 200
    charter = cc.get("/departments/research/charter").json()["charter"]
    assert charter["head"]["prompts"]["system"] == before[0]["body"]


def test_routines_edit_through_the_charter_switch_and_run_now(cc: TestClient, dsn: str) -> None:
    (routine,) = [
        r for r in cc.get("/control/routines").json() if r["key"] == "research:morning-brief"
    ]
    edited = cc.put(f"/control/routines/{routine['id']}", json={"time": "07:05", "days": [1, 3]})
    assert edited.status_code == 200, edited.text
    (after,) = [r for r in cc.get("/control/routines").json() if r["id"] == routine["id"]]
    assert after["time"] == "07:05" and after["days"] == [1, 3]
    charter = cc.get("/departments/research/charter").json()["charter"]
    assert charter["routine"][0]["time"] == "07:05"

    assert cc.put(f"/control/routines/{routine['id']}", json={"time": "25:00"}).status_code == 422
    assert (
        cc.post(f"/control/routines/{routine['id']}/switch", json={"on": False}).json()["enabled"]
        is False
    )

    run = cc.post(f"/control/routines/{routine['id']}/run")
    assert run.status_code == 200, run.text
    (after,) = [r for r in cc.get("/control/routines").json() if r["id"] == routine["id"]]
    assert after["runs"][0]["task_id"] == run.json()["task_id"]
    again = cc.post(f"/control/routines/{routine['id']}/run")
    assert again.json()["task_id"] == run.json()["task_id"], "one press a minute counts once"


def test_tools_keep_their_locks(cc: TestClient, dsn: str, office: Office) -> None:  # noqa: F811
    tools = {t["name"]: t for t in cc.get("/control/tools").json()}
    assert "web_search" in tools
    ok = cc.put("/control/tools/web_search", json={"timeout_seconds": 40})
    assert ok.status_code == 200, ok.text
    lowered = cc.put("/control/tools/web_search", json={"risk_class": "R4"})
    assert lowered.status_code == 200
    (row,) = rows(
        dsn,
        "select approval from public.tools where name = 'web_search' and org_id = %s",
        str(office.org_id),
    )
    assert cc.put("/control/tools/web_search", json={"approval": "auto"}).status_code == 400
    assert row["approval"] == "approval", "R4 always asks"


def test_the_ladder_and_limits(cc: TestClient) -> None:
    assert (
        cc.put(
            "/control/autonomy/rules", json={"level": "L2", "risk_class": "R1", "mode": "gate"}
        ).status_code
        == 200
    )
    assert (
        cc.put(
            "/control/autonomy/rules", json={"level": "L2", "risk_class": "R4", "mode": "run"}
        ).status_code
        == 422
    )
    assert cc.put("/control/autonomy/limits", json={"stuck_task_minutes": 45}).status_code == 200
    body = cc.get("/control/autonomy").json()
    assert {"level": "L2", "risk_class": "R1", "mode": "gate"} in body["rules"]
    assert body["limits"]["stuck_task_minutes"] == 45


def test_models_come_from_the_catalogue(cc: TestClient) -> None:
    assert cc.put("/control/models", json={"tier": "cheap", "model": "made/up"}).status_code == 422
    done = cc.put(
        "/control/models",
        json={"tier": "standard", "model": "openai/gpt-5.6-sol", "department": "research"},
    )
    assert done.status_code == 200, done.text
    body = cc.get("/control/models").json()
    assert {"tier": "standard", "model": "openai/gpt-5.6-sol", "department": "research"} in [
        {k: a[k] for k in ("tier", "model", "department")} for a in body["assignments"]
    ]
    cleared = cc.put("/control/models", json={"tier": "standard", "department": "research"})
    assert cleared.json()["model"] is None


def test_a_retired_rule_stays_on_record_but_leaves_the_map(
    cc: TestClient,
    dsn: str,
    office: Office,  # noqa: F811
) -> None:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        admission = admit(conn, office.org_id)
        fact = conn.execute(
            "insert into public.facts (org_id, claim, source, source_ref, kind, admitted_by) "
            "values (%s, 'No research on crypto tokens', 'owner', 'policy:x', 'rule', %s) "
            "returning id",
            (str(office.org_id), str(admission.request_id)),
        ).fetchone()["id"]
    (rule,) = cc.get("/control/rules").json()
    assert rule["text"] == "No research on crypto tokens" and rule["from"] == "written"

    assert cc.post(f"/control/rules/{fact}/retire").status_code == 200
    assert cc.get("/control/rules").json()[0]["status"] == "retired"
    assert cc.post(f"/control/rules/{fact}/retire").status_code == 404
    claims = [f["claim"] for f in cc.get("/screen/snapshot").json()["facts"]]
    assert "No research on crypto tokens" not in claims


def test_the_change_log_lists_changes_not_work(cc: TestClient, dsn: str, office: Office) -> None:  # noqa: F811
    cc.post("/control/departments/research/budget", json={"daily_budget_usd": 0.6})
    with connect(dsn) as connection, as_service_role(connection) as conn:
        conn.execute(
            "insert into public.events (org_id, type, payload) values (%s, 'model_call', %s)",
            (str(office.org_id), json.dumps({})),
        )
    log = cc.get("/control/log").json()
    types = {e["type"] for e in log}
    assert "department_updated" in types and "charter_activated" in types
    assert "model_call" not in types
    only = cc.get("/control/log", params={"prefix": "charter"}).json()
    assert only and all(e["type"].startswith("charter") for e in only)


def test_a_gate_change_is_a_new_version(cc: TestClient) -> None:
    gates = {g["gate"]: g for g in cc.get("/control/judge").json()["gates"]}
    gate = next(iter(gates))
    done = cc.post(f"/control/judge/{gate}", json={"enabled": False, "note": "quiet week"})
    assert done.status_code == 200, done.text
    versions = cc.get(f"/control/judge/{gate}/versions").json()
    assert versions[0]["enabled"] is False and versions[0]["active"]
    back = cc.post(f"/control/judge/{gate}/activate", json={"version": versions[1]["version"]})
    assert back.status_code == 200


def test_an_agents_tools_and_tier_change_through_its_charter(cc: TestClient) -> None:
    done = cc.post(
        "/control/agents/web-researcher/settings",
        json={"tier": "standard", "tools": ["brain_search", "report_result"]},
    )
    assert done.status_code == 200, done.text
    charter = cc.get("/departments/research/charter").json()["charter"]
    (worker,) = [w for w in charter["workers"] if w["name"] == "web-researcher"]
    assert worker["tier"] == "standard"
    assert worker["allowed_tools"] == ["brain_search", "report_result"]
    (agent,) = [a for a in cc.get("/control/agents").json() if a["name"] == "web-researcher"]
    assert agent["tier"] == "standard" and agent["tools"] == ["brain_search", "report_result"]
    bad = cc.post("/control/agents/web-researcher/settings", json={"tools": ["no_such_tool"]})
    assert bad.status_code == 422


def test_a_new_routine_goes_into_the_charter_switched_off(cc: TestClient) -> None:
    made = cc.post(
        "/control/routines",
        json={
            "department": "research",
            "agent": "research-lead",
            "title": "Friday wrap-up",
            "instructions": "Sum up the week.",
            "time": "16:30",
            "days": [5],
        },
    )
    assert made.status_code == 201, made.text
    (routine,) = [r for r in cc.get("/control/routines").json() if r["name"] == "Friday wrap-up"]
    assert routine["key"] == "research:friday-wrap-up" and routine["enabled"] is False
    assert routine["time"] == "16:30" and routine["days"] == [5]
    wrong = cc.post(
        "/control/routines",
        json={"department": "research", "agent": "chief-of-staff", "title": "Nope routine"},
    )
    assert wrong.status_code == 422
