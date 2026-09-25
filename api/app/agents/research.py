"""The first agent: research Q&A over the brain.

Four steps, one graph node each, so every step is a checkpoint and a crash
costs at most the step in flight:

1. recall   -- find facts in the brain near the question (no model call)
2. answer   -- answer the question, grounded in what was recalled
3. extract  -- pull standalone factual claims out of the answer
4. store    -- propose the new claims to the brain through its write gate
               (ADR 010), with the answer as evidence and the run as provenance

Models are reached only through the gateway and facts only through the
brain. A plain LangGraph graph rather than deepagents: the gateway returns
text and has no tool calling, and a fixed graph is easier to audit than a
model choosing its own next step. Deepagents is reconsidered for the head of
department in Step 7.4.

Prompt text is not in this file: it is configuration the owner edits, read
from the database and pinned per run (see prompts.py).

Each node does its database work in its own transaction, acting as the user
the run was requested by, so RLS applies to the agent exactly as to them.
"""

import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, TypedDict
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.brain import Brain
from app.brain.write_gate import BrainWriter, FactCandidate
from app.gateway import Gateway
from app.judge import JudgeError

AGENT_ROLE = "research"

#: How many facts to recall. Policy, kept here until Step 5 moves thresholds
#: into judge config.
RECALL_LIMIT = 5
#: Per-call output ceilings. Generous on purpose: the cheap tier reasons before
#: it answers, and reasoning counts against max_tokens (see ADR 005).
ANSWER_MAX_TOKENS = 800
EXTRACT_MAX_TOKENS = 800
MAX_CLAIMS = 5

#: The prompts this agent needs, by slot. Their text lives in the database
#: (ADR 007) and is handed to the graph through RunScope.
PROMPT_SLOTS = ("answer", "extract")


class ResearchState(TypedDict, total=False):
    question: str
    recalled: list[dict[str, Any]]
    answer: str
    claims: list[str]
    stored_fact_ids: list[str]
    #: What the write gate decided for each claim, in order.
    fact_writes: list[dict[str, Any]]


@dataclass(frozen=True)
class Session:
    gateway: Gateway
    brain: Brain
    #: The brain's write gate. None when TypeSafe is not configured, in which
    #: case nothing can be written: no fact enters the brain unjudged.
    writer: BrainWriter | None = None


@dataclass(frozen=True)
class RunScope:
    """What every node needs to know about the run it belongs to."""

    run_id: UUID
    org_id: UUID
    agent_id: UUID
    #: Prompt text by slot, as resolved and pinned for this run.
    prompts: Mapping[str, str]
    #: Opens a transaction acting as the run's user and yields the gateway and
    #: brain bound to it. Committed when the block exits cleanly.
    session: Callable[[], AbstractContextManager[Session]]


def build_graph(
    scope: RunScope, *, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    def recall(state: ResearchState) -> ResearchState:
        with scope.session() as s:
            matches = s.brain.search(state["question"], limit=RECALL_LIMIT)
        return {
            "recalled": [
                {"id": str(m.fact.id), "claim": m.fact.claim, "distance": round(m.distance, 4)}
                for m in matches
            ]
        }

    def answer(state: ResearchState) -> ResearchState:
        facts = "\n".join(f"- {f['claim']}" for f in state.get("recalled", [])) or "(none)"
        with scope.session() as s:
            response = s.gateway.complete(
                agent_id=scope.agent_id,
                run_id=scope.run_id,
                max_tokens=ANSWER_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": scope.prompts["answer"]},
                    {
                        "role": "user",
                        "content": f"Known facts:\n{facts}\n\nQuestion: {state['question']}",
                    },
                ],
            )
        return {"answer": response.text.strip()}

    def extract(state: ResearchState) -> ResearchState:
        if not state.get("answer"):
            return {"claims": []}
        with scope.session() as s:
            response = s.gateway.complete(
                agent_id=scope.agent_id,
                run_id=scope.run_id,
                max_tokens=EXTRACT_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": scope.prompts["extract"]},
                    {"role": "user", "content": state["answer"]},
                ],
            )
        return {"claims": parse_claims(response.text)}

    def store(state: ResearchState) -> ResearchState:
        writes: list[dict[str, Any]] = []
        stored: list[str] = []
        with scope.session() as s:
            # Re-running this step after a crash must not duplicate facts or
            # judgments, so claims this run already stored are skipped (a
            # unique index on run and claim backs that up, and held claims
            # are keyed by run and claim in the approval queue).
            already = {c.strip().lower() for c in s.brain.claims_from_run(scope.run_id)}
            for claim in _unique(state.get("claims", [])):
                if claim.strip().lower() in already:
                    continue
                if s.writer is None:
                    writes.append({"claim": claim, "outcome": "not_judged"})
                    continue
                candidate = FactCandidate(
                    claim=claim,
                    source=f"agent:{AGENT_ROLE}",
                    source_text=state.get("answer", ""),
                    source_ref=str(scope.run_id),
                )
                try:
                    result = s.writer.propose(
                        candidate,
                        org_id=scope.org_id,
                        agent_id=scope.agent_id,
                        run_id=scope.run_id,
                    )
                except JudgeError as error:
                    # A gate missing or misconfigured: nothing is written,
                    # and the run says why rather than failing outright.
                    writes.append({"claim": claim, "outcome": "not_judged", "error": error.code})
                    continue
                writes.append(
                    {
                        "claim": claim,
                        "outcome": result.outcome,
                        "fact_id": str(result.fact.id) if result.fact else None,
                    }
                )
                if result.fact is not None:
                    stored.append(str(result.fact.id))
        return {"stored_fact_ids": stored, "fact_writes": writes}

    graph = StateGraph(ResearchState)
    graph.add_node("recall", recall)
    graph.add_node("answer", answer)
    graph.add_node("extract", extract)
    graph.add_node("store", store)
    graph.add_edge(START, "recall")
    graph.add_edge("recall", "answer")
    graph.add_edge("answer", "extract")
    graph.add_edge("extract", "store")
    graph.add_edge("store", END)
    return graph.compile(checkpointer=checkpointer, name="research-agent")


def parse_claims(text: str) -> list[str]:
    """Claims from the model's reply, tolerating fences and stray prose.

    Anything unparseable yields no claims rather than an error: a run that
    stores nothing is recoverable, a run that stores garbage is not.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return []
    try:
        claims = json.loads(match.group(0)).get("claims", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    if not isinstance(claims, list):
        return []
    return [c.strip() for c in claims if isinstance(c, str) and c.strip()][:MAX_CLAIMS]


def _unique(claims: list[str]) -> Iterator[str]:
    seen: set[str] = set()
    for claim in claims:
        key = claim.strip().lower()
        if key and key not in seen:
            seen.add(key)
            yield claim.strip()
