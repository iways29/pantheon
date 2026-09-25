"""Brain maintenance from the command line.

    cd api
    uv run python -m scripts.brain reembed [--batch 64] [--dry-run]

`reembed` brings every fact embedded by an older model (for example the free
`hashing-v1` stand-in used before Step 6) onto the org's current embedding
model, so search can find it again (ADR 013). The claims, their history and
their admissions are unchanged; only the vectors move. It embeds through the
gateway as the `researcher` agent, so it respects the kill switch and the
department budget. Cost with text-embedding-3-small: about $0.02 per million
tokens, so a thousand facts cost well under a cent.
"""

import argparse
import sys

from app.brain import Brain, GatewayEmbedder
from app.config import Settings
from app.db import acting_as, connect
from app.gateway import EMBEDDING_TIER, gateway_from
from scripts.agent import ROOT_ENV, _Env, _setup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    reembed = commands.add_parser("reembed", help="re-embed facts from an older model")
    reembed.add_argument("--batch", type=int, default=64)
    reembed.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, agent_id = _setup(connection)
        with acting_as(connection, user_id=user_id) as conn, conn.cursor() as cursor:
            cursor.execute(
                "select model from public.model_tier_assignments "
                "where org_id = %s and tier = %s and department_id is null",
                (org_id, EMBEDDING_TIER),
            )
            row = cursor.fetchone()
        if row is None:
            raise SystemExit("No embedding model assigned; see scripts.set_tier_model")
        model = row["model"]

        total = 0
        while True:
            with acting_as(connection, user_id=user_id) as conn:
                embedder = GatewayEmbedder(gateway_from(conn, settings), agent_id=agent_id)
                brain = Brain(conn, embedder)
                stale = brain.stale_embeddings(model, limit=args.batch)
                if not stale or args.dry_run:
                    print(f"{len(stale)}{'+' if len(stale) == args.batch else ''} facts to move")
                    break
                total += brain.reembed(stale)
            print(f"re-embedded {total} facts with {model}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
