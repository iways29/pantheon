"""Step 9: Jev checks a worker's result; a failed check redoes the task once
on a stronger tier (ADR 031).

Committed, like test_runners: runs are advanced by `advance_run` on their own
connections. The model and Jev are scripted.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest

from app.agents.runs import Runtime, advance_run
from app.brain.embeddings import HashingEmbedder
from app.db import acting_as, as_service_role, connect
from app.gateway import ModelResponse, UpstreamError
from app.judge.starter_gates import MODEL, RESULT_CHECK
from app.judge.store import seed_gates
from app.tasks import order
from app.tracing import NullTracer
from tests.scripted_jev import ScriptedJev
from tests.test_runners import TIERS, ScriptedTeam, Team, status, team, tick  # noqa: F401


@dataclass
class Recording(ScriptedTeam):
    """ScriptedTeam, remembering which model each call asked for."""

    models: list[str] = field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        self.models.append(model)
        return super().complete(model=model, messages=messages, **kw)


@pytest.fixture
def checked(dsn: str, team: Team) -> Team:  # noqa: F811
    with connect(dsn) as connection:
        with as_service_role(connection) as conn:
            conn.execute(
                "insert into public.model_prices (org_id, provider, model, input_usd_per_mtok) "
                "values (%s, 'typesafe', %s, 0.042)",
                (str(team.org_id), MODEL),
            )
        seed_gates(connection, user_id=team.user_id, org_id=team.org_id, gates=[RESULT_CHECK])
    return team


def runtime(dsn: str, model: Recording, jev: ScriptedJev) -> Runtime:
    return Runtime(
        dsn=dsn,
        transport=model,
        tiers=TIERS,
        embedder=HashingEmbedder(),
        tracer=NullTracer(),
        systemone=jev,
    )


def headcount(dsn: str, where: Team) -> uuid.UUID:
    """An order straight to worker w2, which reports '40 people.'."""
    with connect(dsn) as connection:
        return order(
            connection,
            user_id=where.user_id,
            org_id=where.org_id,
            agent="w2",
            title="Find the headcount",
            instructions="Find the headcount for Acme Corp.",
            idempotency_key=str(uuid.uuid4()),
        ).id


def task_row(dsn: str, task_id: uuid.UUID) -> dict[str, Any]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute("select * from public.tasks where id = %s", (str(task_id),)).fetchone()


def checks(dsn: str, org_id: uuid.UUID) -> list[dict[str, Any]]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return [
            r["payload"]
            for r in conn.execute(
                "select payload from public.events where org_id = %s and type = 'result_checked' "
                "order by created_at",
                (str(org_id),),
            )
        ]


def test_a_good_result_passes_and_the_task_finishes(dsn: str, checked: Team) -> None:
    model, jev = Recording(), ScriptedJev()
    task_id = headcount(dsn, checked)

    (run_id,) = tick(dsn)
    result = advance_run(runtime(dsn, model, jev), run_id, deadline_seconds=60)

    assert (result.status, result.stop_reason) == ("succeeded", "completed"), result.error
    assert status(dsn, task_id) == "done"
    (check,) = checks(dsn, checked.org_id)
    assert check["outcome"] == "pass" and check["escalated_to"] is None
    (call,) = jev.calls_for("unsupported")
    assert '"summary": "40 people."' in call["state"]["result"]
    evidence = call["state"]["evidence"]
    assert evidence.startswith("[given to the agent]\nToday: ")
    assert "Task: Find the headcount Find the headcount for Acme Corp." in evidence
    assert evidence.endswith("(the agent read nothing)")
    assert set(model.models) == {"vendor/small"}


def test_a_failed_check_redoes_the_task_once_on_the_stronger_tier(dsn: str, checked: Team) -> None:
    model, jev = Recording(), ScriptedJev(nouls={"unsupported": 0.9})
    task_id = headcount(dsn, checked)

    (first,) = tick(dsn)
    result = advance_run(runtime(dsn, model, jev), first, deadline_seconds=60)

    assert (result.status, result.stop_reason) == ("succeeded", "escalated"), result.error
    row = task_row(dsn, task_id)
    assert (row["status"], row["model_tier"], row["escalations"]) == ("queued", "standard", 1)
    assert row["result"] is None
    previous = row["input"]["previous_attempt"]
    assert previous["tier"] == "cheap" and previous["problems"] == [
        "Names something not in what the agent read"
    ]

    # The redo: its run carries the stronger tier, and every call uses it.
    (second,) = tick(dsn)
    with connect(dsn) as connection, as_service_role(connection) as conn:
        assert (
            conn.execute(
                "select model_tier from public.runs where id = %s", (str(second),)
            ).fetchone()["model_tier"]
            == "standard"
        )
    calls_before = len(model.models)
    redo = advance_run(runtime(dsn, model, jev), second, deadline_seconds=60)
    assert (redo.status, redo.stop_reason) == ("succeeded", "completed"), redo.error
    assert set(model.models[calls_before:]) == {"vendor/mid"}
    with connect(dsn) as connection, as_service_role(connection) as conn:
        escalated = conn.execute(
            "select payload from public.events where run_id = %s and type = 'model_call'",
            (str(second),),
        ).fetchall()
    # The agent's model calls (not Jev's, which carry no tools field).
    chat = [e["payload"] for e in escalated if "tools_offered" in e["payload"]]
    assert chat and {(e["tier"], e["escalated_from"]) for e in chat} == {("standard", "cheap")}

    # Still flagged, and no redo left: it finishes, marked for whoever reads it.
    row = task_row(dsn, task_id)
    assert row["status"] == "done"
    assert row["result"]["check"] == {"problems": ["Names something not in what the agent read"]}
    first_check, second_check = checks(dsn, checked.org_id)
    assert first_check["escalated_to"] == "standard" and first_check["tier"] == "cheap"
    assert second_check["escalated_to"] is None and second_check["tier"] == "standard"


def test_a_check_that_cannot_run_never_stops_the_work(dsn: str, checked: Team) -> None:
    jev = ScriptedJev(failure=UpstreamError("TypeSafe is down", reason="unavailable"))
    task_id = headcount(dsn, checked)

    (run_id,) = tick(dsn)
    result = advance_run(runtime(dsn, Recording(), jev), run_id, deadline_seconds=60)

    assert (result.status, result.stop_reason) == ("succeeded", "completed"), result.error
    assert status(dsn, task_id) == "done"


def test_no_gate_means_no_check(dsn: str, team: Team) -> None:  # noqa: F811
    jev = ScriptedJev(nouls={"unsupported": 0.9})
    task_id = headcount(dsn, team)

    (run_id,) = tick(dsn)
    advance_run(runtime(dsn, Recording(), jev), run_id, deadline_seconds=60)

    assert status(dsn, task_id) == "done" and jev.calls == []


def test_a_run_tier_never_lowers_the_agents_tier(dsn: str, checked: Team) -> None:
    model = Recording()
    with connect(dsn) as connection, as_service_role(connection) as conn:
        conn.execute(
            "update public.agents set model_tier = 'standard' where id = %s",
            (str(checked.agents["w2"]),),
        )
    task_id = headcount(dsn, checked)
    with connect(dsn) as connection, as_service_role(connection) as conn:
        conn.execute("update public.tasks set model_tier = 'cheap' where id = %s", (str(task_id),))

    (run_id,) = tick(dsn)
    advance_run(runtime(dsn, model, ScriptedJev()), run_id, deadline_seconds=60)

    assert set(model.models) == {"vendor/mid"}


def test_only_the_backend_moves_work_to_another_tier(dsn: str, checked: Team) -> None:
    task_id = headcount(dsn, checked)
    with connect(dsn) as connection:
        for sql in (
            "update public.tasks set model_tier = 'frontier' where id = %s",
            "update public.tasks set escalations = 3 where id = %s",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with acting_as(connection, user_id=str(checked.user_id)) as conn:
                    conn.execute(sql, (str(task_id),))
        (run_id,) = tick(dsn)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with acting_as(connection, user_id=str(checked.user_id)) as conn:
                conn.execute(
                    "update public.runs set model_tier = 'frontier' where id = %s", (str(run_id),)
                )
        # Other changes by a person still work.
        with acting_as(connection, user_id=str(checked.user_id)) as conn:
            conn.execute("update public.tasks set priority = 5 where id = %s", (str(task_id),))


def test_the_report_counts_checks_redos_and_costs(dsn: str, checked: Team) -> None:
    from app.agents.review import summary

    flagged = ScriptedJev(nouls={"unsupported": 0.9})
    redone = headcount(dsn, checked)
    for _ in range(2):
        (run_id,) = tick(dsn)
        advance_run(runtime(dsn, Recording(), flagged), run_id, deadline_seconds=60)
    with connect(dsn) as connection:
        good = order(
            connection,
            user_id=checked.user_id,
            org_id=checked.org_id,
            agent="w1",
            title="Find the founding date",
            instructions="Find the founding date for Acme Corp.",
            idempotency_key=str(uuid.uuid4()),
        ).id
    (run_id,) = tick(dsn)
    advance_run(runtime(dsn, Recording(), ScriptedJev()), run_id, deadline_seconds=60)

    with connect(dsn) as connection:
        report = summary(connection, user_id=checked.user_id, org_id=checked.org_id)

    assert status(dsn, redone) == status(dsn, good) == "done"
    assert (report["tasks_checked"], report["checks"]) == (2, 3)
    assert (report["passed_first_time"], report["redone"], report["still_flagged"]) == (1, 1, 1)
    assert report["problems"] == {"Names something not in what the agent read": 2}
    # Scripted calls cost the same on every tier, so a redo costs what its
    # first attempt did.
    assert report["standard_vs_cheap"] == 1.0
    assert report["cost_usd"]["redos"] > 0


def test_long_evidence_fits_the_gate(dsn: str, checked: Team) -> None:
    from app.judge.store import load_gate

    with connect(dsn) as connection, as_service_role(connection) as conn:
        gate = load_gate(conn, org_id=checked.org_id, gate="result_check")
    evidence = gate.policy.setting("evidence_chars", 0)
    # Evidence full of quotes doubles when escaped; the result adds 4,000.
    assert gate.config.max_state_chars >= evidence * 1.5 + 4000


def test_a_gate_switches_off_and_on_keeping_its_settings(
    dsn: str, checked: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    import scripts.judge as cli
    from app.judge.store import load_gate

    monkeypatch.setattr(
        cli, "_setup", lambda _conn: (str(checked.org_id), str(checked.user_id), None)
    )
    with connect(dsn) as connection:
        for command in ("off", "on"):
            args = argparse.Namespace(gate_command=command, gate="result_check", note=None)
            assert cli._gate(connection, args) == 0
            with as_service_role(connection) as conn:
                live = load_gate(conn, org_id=checked.org_id, gate="result_check").config
            assert live.enabled is (command == "on")
            assert (live.fail_mode, live.max_state_chars) == ("open", 48000)
