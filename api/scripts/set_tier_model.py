"""Point a tier at a different model, with no redeploy.

The owner's tool until the control centre ships in Step 7 (ADR 003). The slug
is checked against OpenRouter's live catalogue before it is saved, and the
change lands in `events` through the table's audit trigger.

    cd api
    uv run python -m scripts.set_tier_model --show
    uv run python -m scripts.set_tier_model cheap deepseek/deepseek-v4-flash-0731
    uv run python -m scripts.set_tier_model standard some/model --department research
    uv run python -m scripts.set_tier_model standard --clear --department research

Connects with DATABASE_URL from the repository's .env and acts as the service
role, so it is for the owner's machine only, never for a request handler.
"""

import argparse
import sys
from pathlib import Path

import psycopg
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import Settings
from app.db import as_service_role, connect
from app.gateway import TIERS, OpenRouterCatalogue, UnknownModel, assign_model, clear_assignment
from app.gateway.factory import tier_map_from

ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class _Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_ENV, extra="ignore")
    database_url: str


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tier", nargs="?", choices=TIERS)
    parser.add_argument("model", nargs="?")
    parser.add_argument("--department", help="department name; omit for the org-wide mapping")
    parser.add_argument("--org", help="org id; defaults to the only org in phase 1")
    parser.add_argument("--clear", action="store_true", help="remove the mapping instead")
    parser.add_argument("--show", action="store_true", help="print the effective mapping")
    args = parser.parse_args(argv)

    with connect(_Env().database_url) as connection, as_service_role(connection):
        with connection.cursor() as cursor:
            org_id = args.org or _only_org(cursor)
            department_id = _department(cursor, org_id, args.department)

        if args.show:
            _show(connection, org_id)
            return 0
        if not args.tier or (not args.model and not args.clear):
            parser.error("give a tier and a model, or a tier with --clear, or --show")

        if args.clear:
            clear_assignment(connection, org_id=org_id, tier=args.tier, department_id=department_id)
        else:
            try:
                assign_model(
                    connection,
                    org_id=org_id,
                    tier=args.tier,
                    model=args.model,
                    catalogue=OpenRouterCatalogue(),
                    department_id=department_id,
                )
            except UnknownModel as error:
                print(error, file=sys.stderr)
                return 1
        _show(connection, org_id)
    return 0


def _only_org(cursor: psycopg.Cursor) -> str:
    cursor.execute("select id from public.orgs order by created_at")
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise SystemExit(f"{len(rows)} orgs exist; pass --org")
    return str(rows[0]["id"])


def _department(cursor: psycopg.Cursor, org_id: str, name: str | None) -> str | None:
    if name is None:
        return None
    cursor.execute(
        "select id from public.departments where org_id = %s and name = %s", (org_id, name)
    )
    row = cursor.fetchone()
    if row is None:
        raise SystemExit(f"No department {name!r} in org {org_id}")
    return str(row["id"])


def _show(connection: psycopg.Connection, org_id: str) -> None:
    defaults = tier_map_from(Settings(_env_file=ROOT_ENV)).models  # type: ignore[call-arg]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select m.tier, coalesce(d.name, '(org-wide)') as scope, m.model
            from public.model_tier_assignments m
            left join public.departments d on d.id = m.department_id
            where m.org_id = %s
            order by m.tier, m.department_id nulls first
            """,
            (org_id,),
        )
        rows = cursor.fetchall()
    set_org_wide = {row["tier"] for row in rows if row["scope"] == "(org-wide)"}
    for tier in TIERS:
        if tier not in set_org_wide:
            print(f"{tier:<9} (org-wide)       {defaults.get(tier, '-')}  [MODEL_TIERS default]")
        for row in rows:
            if row["tier"] == tier:
                print(f"{tier:<9} {row['scope']:<16} {row['model']}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
