"""The judge: typed, logged, threshold-driven decisions from TypeSafe Jev.

`Judge.run(gate, state)` does, in order:

1. The gateway's call gates for the agent the judgment is made for: kill
   switch, department budget, agent sub-cap. A refusal is raised.
2. Loads the gate's live version and questions from the database.
3. Refuses (raises) when the gate is disabled, the state is marked sensitive
   and the gate does not allow it, or the state is over the gate's size limit.
4. Asks every question in one request, through the gateway, so the call is
   costed to the agent's department and logged in model_calls.
5. Writes each raw answer to `judgments`, then applies the gate's policy and
   writes the decision to a `judgment_made` event.

If TypeSafe cannot answer (unreachable, timeout, rate limited, overloaded, a
refused request, or an answer that does not fit the questions), the gate's
fail mode decides: closed takes the most severe outcome, open the least. That
decision is marked `failed` and also written as a `judgment_made` event.

Code decides what to do with the outcome. A confident answer is evidence, not
permission to act.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg

from app.gateway import Gateway, UpstreamError
from app.gateway.gateway import AgentRecord
from app.gateway.systemone import (
    ChoiceAnswer,
    JsonText,
    NoulAnswer,
    Question,
    ScoreAnswer,
    SystemOneResponse,
)
from app.judge.errors import (
    GateDisabled,
    GateMisconfigured,
    JudgeError,
    SensitiveStateRefused,
    StateTooLarge,
    UnknownProfile,
)
from app.judge.policy import Reason
from app.judge.store import Gate, load_gate

AnswerValue = NoulAnswer | ChoiceAnswer | ScoreAnswer


@dataclass(frozen=True)
class Decision:
    gate: str
    gate_version: int
    outcome: str
    reasons: tuple[Reason, ...]
    #: True when TypeSafe gave no usable answer and the fail mode decided.
    failed: bool = False
    #: Why it failed: the transport's reason (`timeout`, `rate_limited`, ...).
    failure: str | None = None
    #: Groups this judgment's rows in `judgments`. None when it failed.
    request_id: UUID | None = None
    #: The versioned model that answered.
    model: str | None = None
    #: The named rule set used, or None for the gate's default rules.
    profile: str | None = None
    answers: Mapping[str, AnswerValue] = field(default_factory=dict)
    input_ref: str | None = None
    cost_usd: float = 0.0
    latency_ms: int | None = None
    tokens_in: int = 0

    def summary(self) -> dict[str, Any]:
        """What the `judgment_made` event carries: the decision, not the text."""
        return {
            "gate": self.gate,
            "gate_version": self.gate_version,
            "outcome": self.outcome,
            "reasons": [
                {"question": r.question, "outcome": r.outcome, "text": r.text} for r in self.reasons
            ],
            "failed": self.failed,
            "failure": self.failure,
            "request_id": str(self.request_id) if self.request_id else None,
            "model": self.model,
            "profile": self.profile,
            "input_ref": self.input_ref,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
        }


class Judge:
    """Runs gates on behalf of agents, over one connection.

    The connection carries the caller's identity, as the gateway's does, so
    RLS decides which gates and agents are visible. Events are written in the
    caller's transaction; the gateway docstring explains what that means for a
    refusal that escapes it.
    """

    def __init__(self, connection: psycopg.Connection, gateway: Gateway) -> None:
        self._connection = connection
        self._gateway = gateway

    def run(
        self,
        gate: str,
        state: JsonText,
        *,
        agent_id: UUID | str,
        run_id: UUID | str | None = None,
        input_ref: str | None = None,
        sensitive: bool = False,
        profile: str | None = None,
        extra_options: dict[str, dict[str, str]] | None = None,
    ) -> Decision:
        """Judge `state` at `gate`. `profile` picks a named rule set, if any.

        `extra_options` adds options to a Choice question for this call only,
        for choices whose options are themselves data (the tools an agent
        may use, the departments that exist). The question's own wording and
        its stored options still come from the database.
        """
        # The kill switch and budgets come before anything else, as they do
        # for every model call.
        agent = self._gateway.admit(agent_id, run_id=run_id)
        try:
            config = load_gate(self._connection, org_id=agent.org_id, gate=gate)
            self._screen(config, state, sensitive=sensitive, profile=profile)
        except JudgeError as error:
            self._emit(agent, run_id, "judgment_refused", error.detail())
            raise

        serialized = _serialize(state)
        reference = input_ref or f"sha256:{hashlib.sha256(serialized.encode()).hexdigest()}"
        questions = config.typed_questions()
        for key, options in (extra_options or {}).items():
            question = questions.get(key)
            if question is None or question.type != "choice":
                raise GateMisconfigured(config.name, [f"{key!r} is not a Choice question"])
            merged = {**(question.criteria or {}), **options}
            if len(merged) > 255:
                raise GateMisconfigured(config.name, [f"{key!r} would have over 255 options"])
            questions[key] = question.model_copy(update={"criteria": merged})

        try:
            response = self._gateway.evaluate(
                agent_id=agent.id,
                model=config.config.model,
                state=state,
                questions=questions,
                run_id=run_id,
            )
            _check_answers(questions, response)
        except UpstreamError as error:
            return self._failed(agent, run_id, config, reference, error, profile)

        request_id = uuid4()
        self._record(agent, run_id, config, reference, request_id, response)
        outcome, reasons = config.policy.decide(response.answers, profile)
        decision = Decision(
            gate=config.name,
            gate_version=config.version,
            outcome=outcome,
            reasons=tuple(reasons),
            request_id=request_id,
            model=response.model,
            profile=profile,
            answers=response.answers,
            input_ref=reference,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            tokens_in=response.tokens_in,
        )
        self._emit(agent, run_id, "judgment_made", decision.summary())
        return decision

    def _screen(
        self, config: Gate, state: JsonText, *, sensitive: bool, profile: str | None
    ) -> None:
        if profile is not None and profile not in config.policy.profiles:
            raise UnknownProfile(config.name, profile, sorted(config.policy.profiles))
        if not config.config.enabled:
            raise GateDisabled(config.name, config.version)
        if sensitive and not config.config.allow_sensitive:
            raise SensitiveStateRefused(config.name)
        size = len(_serialize(state))
        if size > config.config.max_state_chars:
            raise StateTooLarge(config.name, size, config.config.max_state_chars)

    def _failed(
        self,
        agent: AgentRecord,
        run_id: UUID | str | None,
        config: Gate,
        reference: str,
        error: UpstreamError,
        profile: str | None,
    ) -> Decision:
        mode = config.config.fail_mode
        outcome = config.policy.failure_outcome(mode)
        failure = error.reason or error.code
        decision = Decision(
            gate=config.name,
            gate_version=config.version,
            outcome=outcome,
            reasons=(
                Reason(
                    question=None,
                    outcome=outcome,
                    text=f"TypeSafe gave no usable answer ({failure}); the gate fails {mode}",
                ),
            ),
            failed=True,
            failure=failure,
            input_ref=reference,
            profile=profile,
        )
        self._emit(
            agent,
            run_id,
            "judgment_made",
            {**decision.summary(), "error": str(error)[:500], "status": error.status},
        )
        return decision

    def _record(
        self,
        agent: AgentRecord,
        run_id: UUID | str | None,
        config: Gate,
        reference: str,
        request_id: UUID,
        response: SystemOneResponse,
    ) -> None:
        rows = [
            (
                str(agent.org_id),
                str(run_id) if run_id else None,
                config.name,
                key,
                str(config.questions[key].version),
                reference,
                json.dumps(answer.model_dump()),
                str(request_id),
                config.version,
                str(agent.id),
                response.model,
                response.tokens_in,
                response.tokens_out,
                response.latency_ms,
            )
            for key, answer in response.answers.items()
            if key in config.questions
        ]
        with self._connection.cursor() as cursor:
            cursor.executemany(
                """
                insert into public.judgments
                    (org_id, run_id, gate, question_id, question_version, input_ref, output,
                     request_id, gate_version, agent_id, model, tokens_in, tokens_out,
                     latency_ms)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )

    def _emit(
        self,
        agent: AgentRecord,
        run_id: UUID | str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.events (org_id, run_id, agent_id, type, payload)
                values (%s, %s, %s, %s, %s)
                """,
                (
                    str(agent.org_id),
                    str(run_id) if run_id else None,
                    str(agent.id),
                    event_type,
                    json.dumps(payload),
                ),
            )


def _serialize(state: JsonText) -> str:
    if isinstance(state, str):
        return state
    return json.dumps(state, sort_keys=True, ensure_ascii=False)


def _check_answers(questions: Mapping[str, Question], response: SystemOneResponse) -> None:
    """Every question answered, with the type asked for and a known option.

    The transport checks the shape of the body; this checks it against what
    was asked. Anything off is treated like an outage: the fail mode decides.
    """
    problems: list[str] = []
    for key, question in questions.items():
        answer = response.answers.get(key)
        if answer is None:
            problems.append(f"{key}: missing")
            continue
        if answer.type != question.type:
            problems.append(f"{key}: asked {question.type}, got {answer.type}")
            continue
        if isinstance(answer, ChoiceAnswer) and isinstance(question.criteria, dict):
            if answer.choice not in question.criteria:
                problems.append(f"{key}: chose unknown option {answer.choice!r}")
        if isinstance(answer, ScoreAnswer) and isinstance(question.criteria, list):
            if not 0 <= answer.score <= len(question.criteria) - 1:
                problems.append(f"{key}: score {answer.score} outside the levels")
    if problems:
        raise UpstreamError(
            f"TypeSafe answers do not fit the questions: {'; '.join(problems)}",
            reason="malformed",
        )
