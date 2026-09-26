"""Give the Chief of Staff an order from the command line (Step 8.2, ADR 027).

    cd api
    uv run python -m scripts.order "Find out which space-tech funds raised money this month"
    uv run python -m scripts.order --status <task-id>

The Chief of Staff routes it to a department on the scheduler's next tick,
or asks you (python -m scripts.approvals list) when it is not sure.
"""

import argparse
import json
import sys

from app.db import connect
from app.owner_api import give_order
from app.tasks import tree
from scripts.agent import _Env, _setup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("text", nargs="?")
    parser.add_argument("--status", metavar="TASK_ID")
    args = parser.parse_args(argv)
    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, _ = _setup(connection)
        if args.status:
            for row in tree(connection, user_id=user_id, root_task_id=args.status):
                summary = (row["result"] or {}).get("summary", "")
                print(
                    f"{'  ' * row['depth']}{row['title']}  [{row['status']}]  "
                    f"${row['tree_cost_usd']}  {summary[:120]}"
                )
            return 0
        if not args.text:
            parser.error("give the order text, or --status TASK_ID")
        task = give_order(connection, user_id, org_id, args.text)
        print(json.dumps({"task": str(task.id), "status": task.status, "new": task.created}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
