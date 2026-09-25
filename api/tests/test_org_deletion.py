"""Deleting an org removes everything it owns, audit triggers included.

Every audit trigger that fires on delete writes an event for the row's org.
When the org itself is being deleted, the cascade reaches those rows after
the org is gone, so an unguarded trigger fails on events_org_id_fkey and
aborts the whole delete.
"""

import psycopg

from app.db import as_service_role
from tests.conftest_db import Tenants
from tests.test_gateway import make_agent


def test_an_org_with_tier_assignments_triggers_and_prices_can_be_deleted(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent_id = make_agent(db, tenants.org_a)
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into public.model_tier_assignments (org_id, tier, model) "
            "values (%s, 'cheap', 'vendor/small-model')",
            (str(tenants.org_a),),
        )
        cursor.execute(
            """
            insert into public.triggers
                (org_id, agent_id, name, task, time_of_day, timezone, run_as)
            values (%s, %s, 'morning', '{"question": "q"}', '08:00', 'UTC', %s)
            """,
            (str(tenants.org_a), str(agent_id), str(tenants.user_a)),
        )
        cursor.execute(
            "insert into public.model_prices (org_id, provider, model, input_usd_per_mtok) "
            "values (%s, 'typesafe', 'jev-1.13.0', 0.042)",
            (str(tenants.org_a),),
        )

        cursor.execute("delete from public.orgs where id = %s", (str(tenants.org_a),))

        for table in ("orgs", "model_tier_assignments", "triggers", "model_prices", "events"):
            column = "id" if table == "orgs" else "org_id"
            cursor.execute(
                f"select count(*) as n from public.{table} where {column} = %s",
                (str(tenants.org_a),),
            )
            assert cursor.fetchone()["n"] == 0, table


def test_deleting_one_tier_assignment_is_still_audited(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    """The guard skips only cascades from a deleted org, not ordinary deletes."""
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into public.model_tier_assignments (org_id, tier, model) "
            "values (%s, 'cheap', 'vendor/small-model')",
            (str(tenants.org_a),),
        )
        cursor.execute(
            "delete from public.model_tier_assignments where org_id = %s",
            (str(tenants.org_a),),
        )
        cursor.execute(
            "select payload from public.events "
            "where org_id = %s and type = 'model_tier_changed' "
            "and payload->>'operation' = 'delete'",
            (str(tenants.org_a),),
        )
        rows = cursor.fetchall()

    assert len(rows) == 1
    assert rows[0]["payload"]["from"] == "vendor/small-model"
    assert rows[0]["payload"]["to"] is None
