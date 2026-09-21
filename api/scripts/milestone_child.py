"""Advance one run in its own process, so the Milestone can SIGKILL it mid-run.

    uv run python -m scripts.milestone_child <run-id>

Used only by scripts/milestone.py. A separate process is the honest way to
show a crash: SIGKILL gives the run no chance to tidy up, record a status or
release its lease, exactly as a serverless instance killed mid-step would not.
"""

import sys

from app.agents.runs import Runtime, advance_run
from app.brain.embeddings import HashingEmbedder
from app.config import Settings
from app.gateway.factory import tier_map_from, transport_from
from app.tracing import tracer_from
from scripts.agent import ROOT_ENV, _Env


def main(run_id: str) -> int:
    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    runtime = Runtime(
        dsn=_Env().database_url,
        transport=transport_from(settings),
        tiers=tier_map_from(settings),
        embedder=HashingEmbedder(),
        tracer=tracer_from(settings),
    )
    advance_run(runtime, run_id, deadline_seconds=240)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
