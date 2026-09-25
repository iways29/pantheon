"""Input and output guardrails (Step 5.3).

Every message going into an agent (`input`) and everything an agent
produces for the outside world (`output`) can be checked in one TypeSafe
request: hazard Nouls plus a severity Score, after TypeSafe's LLM guardrails
cookbook. The gates `guard_input` and `guard_output` map the answers to
`pass`, `review` or `block` under a named policy: `strict` (the default) or
`normal`, both held as thresholds in the gate's configuration.

A policy is only numbers over the same answers, so `reroute` decides a stored
assessment under another policy with no new model call.

One rule is also checked in code, because it is exact and Jev need not guess
it: agent-written output containing Devanagari script is blocked (owner,
2026-09-26: all content is in English; verses appear only on the website,
from the vetted library). The block survives a reroute.
"""

import dataclasses
import json
import re
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

_DEVANAGARI = re.compile(r"[\u0900-\u097F\uA8E0-\uA8FF]")
#: Marks a reason that came from a code check rather than from Jev's answers.
CODE_CHECK = "code check: "
DEVANAGARI_REASON = f"{CODE_CHECK}Devanagari script in the output; all content is in English"


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
        decision = self._judge.run(
            GATES[side],
            state,
            agent_id=agent_id,
            run_id=run_id,
            sensitive=sensitive,
            profile=profile,
        )
        if side == "output" and _DEVANAGARI.search(text):
            overridden = decision.outcome
            decision = dataclasses.replace(
                decision,
                outcome="block",
                reasons=(Reason(None, "block", DEVANAGARI_REASON), *decision.reasons),
            )
            # The judgment_made event holds Jev's outcome; this one records
            # that code overrode it, so the trail matches what happened.
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into public.events (org_id, run_id, agent_id, type, payload)
                    select a.org_id, %s, a.id, 'guardrail_code_block', %s
                    from public.agents a where a.id = %s
                    """,
                    (
                        str(run_id) if run_id else None,
                        json.dumps(
                            {
                                "gate": decision.gate,
                                "request_id": str(decision.request_id)
                                if decision.request_id
                                else None,
                                "judged": overridden,
                                "outcome": "block",
                                "reason": DEVANAGARI_REASON,
                            }
                        ),
                        str(agent_id),
                    ),
                )
        return decision

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
        outcome, reasons = gate.policy.decide(decision.answers, profile)
        coded = [r for r in decision.reasons if r.text.startswith(CODE_CHECK)]
        if coded:
            return "block", [*coded, *reasons]
        return outcome, reasons
