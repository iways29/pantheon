"""Add documents to the agents' library and search it.

    cd api
    uv run python -m scripts.knowledge add path/to/brief.md --title "Company brief" --scope company
    uv run python -m scripts.knowledge add playbook.md --title "Playbook" \
        --scope department --department marketing
    uv run python -m scripts.knowledge add notes.md --title "Notes" --scope agent --agent writer
    uv run python -m scripts.knowledge search "how often do we post" [--as writer]

Every upload is screened before it is chunked (ADR 011, 015); the original
file goes to the private `documents` bucket in Supabase Storage. Screening and
embeddings are paid by `--processed-by` (default: researcher). `search --as`
shows what that agent would see; without it, what the owner sees.
"""

import argparse
import mimetypes
import sys
from pathlib import Path

from app.brain import GatewayEmbedder
from app.config import Settings
from app.db import acting_as, connect
from app.gateway import gateway_from
from app.knowledge.library import Library
from app.knowledge.wiring import library_from
from scripts.agent import ROOT_ENV, _Env, _setup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("path", type=Path)
    add.add_argument("--title", required=True)
    add.add_argument("--scope", choices=["company", "department", "agent"], required=True)
    add.add_argument("--department")
    add.add_argument("--agent")
    add.add_argument("--processed-by", default="researcher")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--as", dest="as_agent")
    search.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, researcher_id = _setup(connection)
        if args.command == "add":
            agent_id = _agent_id(connection, user_id, org_id, args.processed_by)
            content_type = mimetypes.guess_type(args.path.name)[0] or "text/plain"
            if args.path.suffix == ".md":
                content_type = "text/markdown"
            with acting_as(connection, user_id=user_id) as conn:
                result = library_from(conn, settings, processor_agent_id=agent_id).add(
                    org_id=org_id,
                    content=args.path.read_bytes(),
                    filename=args.path.name,
                    content_type=content_type,
                    title=args.title,
                    scope=args.scope,
                    department=args.department,
                    agent=args.agent,
                    processor_agent_id=agent_id,
                )
            state = "added" if result.created else "already there"
            print(f"{args.title}: {state}, {result.status}, {result.chunks} chunks")
            for reason in result.reasons:
                print(f"  {reason}")
            return 0

        agent_id = _agent_id(connection, user_id, org_id, args.as_agent) if args.as_agent else None
        payer = agent_id or researcher_id
        with acting_as(connection, user_id=user_id, agent_id=agent_id) as conn:
            embedder = GatewayEmbedder(gateway_from(conn, settings), agent_id=payer)
            for match in Library(conn, embedder=embedder).search(args.query, limit=args.limit):
                print(f"{match.distance:.3f}  [{match.scope}] {match.title} #{match.position}")
                print(f"       {match.text[:160]}")
    return 0


def _agent_id(connection: object, user_id: str, org_id: str, name: str) -> str:
    with acting_as(connection, user_id=user_id) as conn, conn.cursor() as cursor:  # type: ignore[arg-type]
        cursor.execute(
            "select id from public.agents where org_id = %s and name = %s", (org_id, name)
        )
        row = cursor.fetchone()
    if row is None:
        raise SystemExit(f"No agent {name!r}")
    return str(row["id"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
