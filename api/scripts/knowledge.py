"""Add documents to the agents' library and search it.

    cd api
    uv run python -m scripts.knowledge add path/to/brief.md --title "Company brief" --scope company
    uv run python -m scripts.knowledge add playbook.md --title "Playbook" \
        --scope department --department marketing
    uv run python -m scripts.knowledge add notes.md --title "Notes" --scope agent --agent writer
    uv run python -m scripts.knowledge search "how often do we post" [--as writer]
    uv run python -m scripts.knowledge link preview https://example.com/about [--as researcher]
    uv run python -m scripts.knowledge link push <preview-id>

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
from app.knowledge.fetch import FetchRefused, fetch
from app.knowledge.library import Library
from app.knowledge.links import LinkError
from app.knowledge.wiring import library_from, links_from
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
    link = commands.add_parser("link", help="preview a web page, then push it to the brain")
    link_commands = link.add_subparsers(dest="link_command", required=True)
    link_preview = link_commands.add_parser("preview")
    link_preview.add_argument("url")
    link_preview.add_argument("--as", dest="as_agent", default="researcher")
    link_push = link_commands.add_parser("push")
    link_push.add_argument("preview_id")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, researcher_id = _setup(connection)
        if args.command == "link":
            return _link(connection, settings, args, org_id, user_id)
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


def _link(
    connection: object, settings: Settings, args: argparse.Namespace, org_id: str, user_id: str
) -> int:
    if args.link_command == "preview":
        agent_id = _agent_id(connection, user_id, org_id, args.as_agent)
        try:
            page = fetch(args.url)
        except FetchRefused as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1
        with acting_as(connection, user_id=user_id) as conn:  # type: ignore[arg-type]
            preview = links_from(conn, settings, agent_id=agent_id).preview(
                page, org_id=org_id, agent_id=agent_id
            )
        print(f"preview {preview.id}  {preview.final_url}  screened {preview.label}")
        for reason in preview.reasons:
            print(f"  {reason}")
        for claim in preview.claims:
            print(f"  would add: {claim}")
        if preview.label == "clean" and preview.claims:
            print(f"push with: python -m scripts.knowledge link push {preview.id}")
        return 0
    with acting_as(connection, user_id=user_id) as conn, conn.cursor() as cursor:  # type: ignore[arg-type]
        cursor.execute(
            "select agent_id from public.link_previews where id = %s", (args.preview_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise SystemExit(f"No preview {args.preview_id}")
        try:
            pushed = links_from(conn, settings, agent_id=row["agent_id"]).push(
                args.preview_id, org_id=org_id
            )
        except LinkError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1
    for result in pushed.results:
        print(f"{result['outcome']:<10} {result['claim']}")
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
