"""Step 9: the side-by-side comparison of cheap, cascade and standard (ADR 032)."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from app.agents.benchmark import Case, compare, ensure_agent, load_cases, quality
from app.db import as_service_role
from app.gateway import Gateway, ModelResponse
from app.judge.starter_gates import RESULT_CHECK
from app.judge.store import seed_gates
from tests.conftest_db import Tenants
from tests.scripted_jev import ScriptedJev, noul
from tests.test_judge import set_price
from tests.test_runners import TIERS

CASES = Path(__file__).parents[1] / "evals" / "cascade" / "scouting.json"
LUMEN = Case(
    "lumen",
    [
        "[techcrunch.com/a] Lumen, founded by Priya Rao, raised a $4M seed. "
        "Its lead has a $400M fund."
    ],
    ["Lumen", "Priya Rao", "$4M"],
    ["$400M"],
)
QUIET = Case("quiet", ["[techcrunch.com/b] The EU published AI guidance."], [], ["$"])


@dataclass
class Tiers:
    """Cheap answers take the fund size for the round; standard gets it right."""

    calls: list[str] = field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        self.calls.append(model)
        user = messages[-1]["content"]
        if "Lumen" not in user:
            text = "Nothing in these pages qualifies."
        elif model == TIERS.models["cheap"]:
            text = "Lumen (Priya Rao) raised $400M."
        else:
            text = "Lumen, founded by Priya Rao, raised a $4 million seed."
        return ModelResponse(
            model=model,
            text=text,
            tokens_in=500,
            tokens_out=50,
            cost_usd=0.0001 if model == TIERS.models["cheap"] else 0.003,
            latency_ms=1,
            provider="scripted",
        )


def test_the_labelled_scouting_cases_load_and_score() -> None:
    today, task, cases = load_cases(CASES)
    assert today == "2026-09-28" and "30 days" in task and len(cases) == 8
    # Every case's gold is in its own pages: a perfect answer is possible.
    for case in cases:
        pages = " ".join(case.pages)
        assert quality(pages, case) >= 0 and all(g in pages for g in case.gold), case.id


def test_quality_counts_gold_and_traps() -> None:
    assert quality("Lumen, founded by Priya Rao, raised a $4 million seed.", LUMEN) == 1.0
    assert quality("Lumen (Priya Rao) raised $400M.", LUMEN) == 0.167
    assert quality("Nothing qualifies.", QUIET) == 1.0
    assert quality("Acme raised $2M.", QUIET) == 0.5


def test_the_cascade_redoes_what_jev_flags_and_the_report_says_so(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    set_price(db, tenants.org_a)
    seed_gates(db, user_id=tenants.user_a, org_id=tenants.org_a, gates=[RESULT_CHECK])
    agent_id = ensure_agent(db, user_id=tenants.user_a, org_id=tenants.org_a)
    assert ensure_agent(db, user_id=tenants.user_a, org_id=tenants.org_a) == agent_id
    model = Tiers()
    # Jev flags an amount not in the pages, which only the cheap Lumen answer has.
    jev = ScriptedJev(
        respond=lambda state, q: {"unsupported": noul(0.9 if "$400M." in state["result"] else 0.05)}
    )

    result = compare(
        db,
        lambda conn: Gateway(conn, model, TIERS, systemone=jev),
        user_id=tenants.user_a,
        org_id=tenants.org_a,
        agent_id=agent_id,
        today="2026-09-28",
        task="Scout early AI startups.",
        cases=[LUMEN, QUIET],
        label="test",
    )

    lumen, quiet = result.rows
    assert lumen.redo and lumen.cascade_quality == lumen.standard_quality == 1.0
    assert not quiet.redo and quiet.cascade_quality == 1.0
    totals = result.totals()
    assert totals["quality"] == {"cheap": 0.584, "cascade": 1.0, "standard": 1.0}
    # The cascade paid for one standard answer of two, plus the checks.
    assert totals["cost_per_task_usd"]["cascade"] < totals["cost_per_task_usd"]["standard"]
    assert result.verdict().startswith("Done")
    assert "| lumen | 0.17 | 1.00 | 1.00 | yes: Names something" in result.markdown()
    assert model.calls.count(TIERS.models["standard"]) == 2
    with as_service_role(db) as conn:
        runs = conn.execute(
            "select model_tier, status from public.runs where agent_id = %s", (str(agent_id),)
        ).fetchall()
        event = conn.execute(
            "select payload from public.events where type = 'cascade_compared'"
        ).fetchone()
    assert sorted(str(r["model_tier"]) for r in runs) == ["None", "None", "standard", "standard"]
    assert {r["status"] for r in runs} == {"succeeded"}
    assert event["payload"]["redone"] == 1
