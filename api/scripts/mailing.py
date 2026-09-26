"""Mailing lists and emails from the command line (ADR 028).

    cd api
    uv run python -m scripts.mailing show
    uv run python -m scripts.mailing set morning-brief \\
        --from "The Unreal Lab <newsletter@example.com>" --to you@example.com
    uv run python -m scripts.mailing set morning-brief --to a@example.com,b@example.com
    uv run python -m scripts.mailing set morning-brief --auto      # goes out on its own
    uv run python -m scripts.mailing set morning-brief --no-auto   # each email waits for you
    uv run python -m scripts.mailing emails                        # the last emails
    uv run python -m scripts.mailing send <email id>               # retry a failed one

`set` changes only what is passed. The same lists are behind the owner API
(`PUT /mailing-lists/{key}`) and, later, the recipients form.
"""

import argparse
import sys

from app.config import Settings
from app.db import acting_as, connect
from app.mail import MailingListChange, get_lists, mailer_from, send, set_list
from scripts.agent import ROOT_ENV, _Env, _setup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    change = commands.add_parser("set")
    change.add_argument("key")
    change.add_argument("--from", dest="from_address")
    change.add_argument("--to", help="comma-separated; replaces the recipients")
    change.add_argument("--reply-to")
    change.add_argument("--subject", help="{date} becomes the day it is written")
    change.add_argument("--timezone")
    change.add_argument("--auto", dest="auto", action="store_true", default=None)
    change.add_argument("--no-auto", dest="auto", action="store_false")
    change.add_argument("--on", dest="enabled", action="store_true", default=None)
    change.add_argument("--off", dest="enabled", action="store_false")
    commands.add_parser("emails")
    retry = commands.add_parser("send")
    retry.add_argument("email_id")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, _ = _setup(connection)
        if args.command == "show":
            for row in get_lists(connection, user_id=user_id):
                auto = "on its own" if row["send_without_approval"] else "waits for approval"
                state = "on" if row["enabled"] else "off"
                print(f"{row['key']} ({state}, {auto})")
                print(f"  from: {row['from_address']}")
                print(f"  to:   {', '.join(row['recipients']) or '(nobody yet)'}")
                print(f"  subject: {row['subject']}  [{row['timezone']}]")
            return 0
        if args.command == "set":
            row = set_list(
                connection,
                user_id=user_id,
                org_id=org_id,
                key=args.key,
                change=MailingListChange(
                    from_address=args.from_address,
                    recipients=args.to.split(",") if args.to is not None else None,
                    reply_to=args.reply_to,
                    subject=args.subject,
                    timezone=args.timezone,
                    send_without_approval=args.auto,
                    enabled=args.enabled,
                ),
            )
            print(f"{row['key']}: from {row['from_address']} to {', '.join(row['recipients'])}")
            return 0
        if args.command == "emails":
            with acting_as(connection, user_id=user_id) as conn:
                rows = conn.execute(
                    "select id, status, subject, error, created_at from public.emails "
                    "order by created_at desc limit 20"
                ).fetchall()
            for r in rows:
                print(
                    f"{r['created_at']:%Y-%m-%d %H:%M}  {r['status']:<9} {r['subject']}  {r['id']}"
                )
                if r["error"]:
                    print(f"    {r['error']}")
            return 0
        print(send(connection, mailer_from(settings), args.email_id))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
