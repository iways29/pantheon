"""Input and output guardrails (Step 5.3).

Every message going into an agent (`input`) and everything an agent
produces for the outside world (`output`) can be checked in one TypeSafe
request: hazard Nouls plus a severity Score, after TypeSafe's LLM guardrails
cookbook. The gates `guard_input` and `guard_output` map the answers to
`pass`, `review` or `block` under a named policy: `strict` (the default) or
`normal`, both held as thresholds in the gate's configuration.

A policy is only numbers over the same answers, so `reroute` decides a stored
assessment under another policy with no new model call.
"""

from typing import Any, Literal
from uuid import UUID

import psycopg

from app.gateway.systemone import JsonText
from app.judge import Decision, Judge
from app.judge.policy import Reason
from app.judge.store import load_gate

Side = Literal["input", "output"]
GATES: dict[str, str] = {"input": "guard_input", "output": "guard_output"}
DEFAULT_PROFILE = "strict"


class Guard:
    def __init__(self, connection: psycopg.Connection, judge: Judge) -> None:
        self._connection = connection
        self._judge = judge

    def check(
        self,
        text: str,
        *,
        side: Side,
        agent_id: UUID | str,
        profile: str = DEFAULT_PROFILE,
        context: JsonText | None = None,
        run_id: UUID | str | None = None,
        sensitive: bool = False,
    ) -> Decision:
        """Assess `text` and route it under `profile`. Refusals are raised."""
        state: dict[str, Any] = {"text": text}
        if context is not None:
            state["context"] = context
        return self._judge.run(
            GATES[side],
            state,
            agent_id=agent_id,
            run_id=run_id,
            sensitive=sensitive,
            profile=profile,
        )

    def reroute(
        self, decision: Decision, *, org_id: UUID | str, profile: str
    ) -> tuple[str, list[Reason]]:
        """The same assessment under another policy. No model call.

        Uses the gate's live thresholds. A failed assessment has no answers
        to reroute, so it keeps its fail-mode outcome.
        """
        if decision.failed:
            return decision.outcome, list(decision.reasons)
        gate = load_gate(self._connection, org_id=org_id, gate=decision.gate)
        return gate.policy.decide(decision.answers, profile)
