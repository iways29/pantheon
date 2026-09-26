"""The owner's content desk from the command line (Step 8.3, ADR 029).

    cd api
    uv run python -m scripts.draft list [--status ready]
    uv run python -m scripts.draft show <draft_id>
    uv run python -m scripts.draft approve <draft_id> [--edit-file post.txt] [--note "..."]
    uv run python -m scripts.draft reject <draft_id> [--note "why"]
    uv run python -m scripts.draft posted <draft_id> --url https://...
    uv run python -m scripts.draft trace <fact_id>            # every draft that used a fact
    uv run python -m scripts.draft public <fact_id> [--internal]

Approving with an edited text keeps your version as an example of your voice.
Phase 1: you post by hand, then mark the draft as posted.
"""

import argparse
import sys
from pathlib import Path

from app.approvals import decide
from app.db import acting_as, connect
from scripts.agent import _Env, _setup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument(
        "--status", choices=["draft", "blocked", "ready", "approved", "rejected", "published"]
    )
    listing.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show")
    show.add_argument("draft_id")
    approve = commands.add_parser("approve")
    approve.add_argument("draft_id")
    approve.add_argument("--edit-file", type=Path, help="your edited text, from a file")
    approve.add_argument("--note")
    reject = commands.add_parser("reject")
    reject.add_argument("draft_id")
    reject.add_argument("--note")
    posted = commands.add_parser("posted")
    posted.add_argument("draft_id")
    posted.add_argument("--url", required=True)
    trace = commands.add_parser("trace")
    trace.add_argument("fact_id")
    public = commands.add_parser("public", help="clear a fact for public content")
    public.add_argument("fact_id")
    public.add_argument("--internal", action="store_true", help="make it internal again")
    args = parser.parse_args(argv)

    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, _ = _setup(connection)
        if args.command in ("approve", "reject"):
            with acting_as(connection, user_id=user_id) as conn:
                row = conn.execute(
                    "select approval_id, status from public.drafts where id = %s",
                    (args.draft_id,),
                ).fetchone()
            if row is None or row["approval_id"] is None or row["status"] != "ready":
                print(f"Draft {args.draft_id} is not waiting for you ({row and row['status']}).")
                return 1
            edited = None
            if args.command == "approve" and args.edit_file:
                edited = {"body": args.edit_file.read_text().strip()}
            decision = (
                "approve" if args.command == "approve" else ("redirect" if args.note else "cancel")
            )
            decide(
                connection,
                user_id=user_id,
                approval_id=row["approval_id"],
                decision=decision,
                note=args.note,
                edited_arguments=edited,
            )
            print(f"{args.draft_id}  {'approved' if decision == 'approve' else 'rejected'}")
            return 0

        with acting_as(connection, user_id=user_id) as conn, conn.cursor() as cursor:
            if args.command == "list":
                cursor.execute(
                    "select id, status, channel, format, title, created_at::date as day, "
                    "checks->>'voice_score' as voice from public.drafts "
                    "where org_id = %s and (%s::text is null or status = %s::text) "
                    "order by created_at desc limit %s",
                    (org_id, args.status, args.status, args.limit),
                )
                for r in cursor.fetchall():
                    print(
                        f"{r['id']}  {r['day']}  {r['status']:<9} {r['channel']:<10} "
                        f"{r['format']:<9} voice {r['voice'] or '-':<5} {r['title']}"
                    )
            elif args.command == "show":
                cursor.execute("select * from public.drafts where id = %s", (args.draft_id,))
                d = cursor.fetchone()
                if d is None:
                    print("No such draft.")
                    return 1
                print(f"{d['title']}  [{d['channel']} {d['format']}, {d['status']}]\n")
                print(d["owner_body"] or d["body"])
                for item in (d["checks"] or {}).get("blocking", []):
                    print(f"\nBLOCKED  {item.get('sentence', '')}\n         {item['reason']}")
                    for hint in item.get("internal_facts", []):
                        print(f"         internal fact {hint['fact_id']}: {hint['claim']}")
                for item in (d["checks"] or {}).get("flags", []):
                    print(f"\nLOOK AT  {item.get('sentence', '')}  {item['reason']}")
                cursor.execute(
                    "select c.relation, c.verdict, f.id, f.claim from public.artifact_claims c "
                    "join public.facts f on f.id = c.fact_id where c.draft_id = %s",
                    (args.draft_id,),
                )
                for r in cursor.fetchall():
                    print(
                        f"\nfact {r['relation']:<9} {r['verdict'] or '':<10} "
                        f"{r['id']}  {r['claim']}"
                    )
            elif args.command == "posted":
                cursor.execute(
                    "update public.drafts set status = 'published', published_url = %s, "
                    "published_at = now() where id = %s returning status",
                    (args.url, args.draft_id),
                )
                print(f"{args.draft_id}  {cursor.fetchone()['status']}")
            elif args.command == "trace":
                cursor.execute(
                    "select distinct d.id, d.status, d.channel, d.title, d.published_url "
                    "from public.artifact_claims c join public.drafts d on d.id = c.draft_id "
                    "where c.fact_id = %s order by d.status",
                    (args.fact_id,),
                )
                found = cursor.fetchall()
                for r in found:
                    print(
                        f"{r['id']}  {r['status']:<9} {r['channel']:<10} {r['title']}  "
                        f"{r['published_url'] or ''}"
                    )
                if not found:
                    print("No draft used this fact.")
            elif args.command == "public":
                cursor.execute(
                    "update public.facts set visibility = %s where id = %s returning claim",
                    ("internal" if args.internal else "public", args.fact_id),
                )
                row = cursor.fetchone()
                print(row["claim"] if row else "No such fact.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
