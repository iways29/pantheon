"""The brain's graph: set up, switch on, look inside (ADR 035).

    cd api
    uv run python -m scripts.graph setup            # the librarian, its budget and routine (off)
    uv run python -m scripts.graph on | off         # the librarian's daily routine
    uv run python -m scripts.graph now              # read pending facts now (a task)
    uv run python -m scripts.graph stats            # things, links, unlinked, pending
    uv run python -m scripts.graph show "Mumba.ai"  # a thing and what it links to

The kinds and relations it may use are the `graph_schema` flag.
"""

import argparse
import json
import sys
import uuid

from app.agents.librarian import AGENT, ROUTINE, ensure_librarian
from app.db import acting_as, connect
from app.tasks import order
from scripts.agent import _Env, _setup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "on", "off", "now", "stats"):
        commands.add_parser(name)
    show = commands.add_parser("show")
    show.add_argument("name")
    args = parser.parse_args(argv)

    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, _ = _setup(connection)
        if args.command == "setup":
            print(
                json.dumps(
                    ensure_librarian(connection, user_id=user_id, org_id=org_id), default=str
                )
            )
            return 0
        if args.command in ("on", "off"):
            with acting_as(connection, user_id=user_id) as conn:
                conn.execute(
                    "update public.triggers set enabled = %s where org_id = %s and name = %s",
                    (args.command == "on", org_id, ROUTINE),
                )
            print(f"{ROUTINE}: {args.command}")
            return 0
        if args.command == "now":
            task = order(
                connection,
                user_id=user_id,
                org_id=org_id,
                agent=AGENT,
                title="Read new facts into the graph",
                idempotency_key=f"graph-now:{uuid.uuid4()}",
            )
            print(f"queued {task.id}")
            return 0
        with acting_as(connection, user_id=user_id) as conn:
            if args.command == "stats":
                row = conn.execute(
                    """
                    select
                      (select count(*) from public.entities where status = 'active') as things,
                      (select count(*) from public.entities where status = 'merged') as merged,
                      (select count(*) from public.entity_links where status = 'active') as links,
                      (select count(*) from public.facts where graph_state = 'pending'
                         and status = 'active') as pending,
                      (select count(*) from public.entities e where e.status = 'active'
                         and not exists (select 1 from public.entity_links l
                                          where l.from_entity = e.id or l.to_entity = e.id))
                        as unlinked
                    """
                ).fetchone()
                print(json.dumps(dict(row)))
                return 0
            thing = conn.execute(
                "select id, kind, name, aliases, description from public.entities "
                "where status = 'active' and (lower(name) = lower(%s) or lower(%s) = any("
                "select lower(a) from unnest(aliases) a)) limit 1",
                (args.name, args.name),
            ).fetchone()
            if thing is None:
                print(f"No thing called {args.name!r}")
                return 1
            print(f"{thing['name']} ({thing['kind']}) {thing['description']}")
            if thing["aliases"]:
                print(f"  also: {', '.join(thing['aliases'])}")
            for r in conn.execute(
                """
                select l.relation, a.name as a, b.name as b, f.claim
                  from public.entity_links l
                  join public.entities a on a.id = l.from_entity
                  join public.entities b on b.id = l.to_entity
                  join public.facts f on f.id = l.fact_id
                 where l.status = 'active' and (l.from_entity = %s or l.to_entity = %s)
                """,
                (thing["id"], thing["id"]),
            ):
                print(f"  {r['a']} -{r['relation']}-> {r['b']}   [{r['claim'][:80]}]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
