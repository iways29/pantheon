"""Gates and questions, read from and written to the database.

The only module that touches `judge_gates` and `judge_questions`. Like agent
prompts (ADR 007), every edit is a new version, older versions are history
that cannot be rewritten, one version is live, and each change of which one is
live is written to `events` by a trigger. A change takes effect on the next
judgment with no deploy.

Reads use the caller's connection as it stands, so RLS decides which org's
gates are visible. Writes act as the named user, as prompt edits do.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from pydantic import ValidationError

from app.db import acting_as
from app.gateway.systemone import JsonText, Question
from app.judge.errors import GateMisconfigured, GateNotConfigured
from app.judge.policy import FailMode, Policy
from app.judge.starter_gates import MODEL as STARTER_MODEL
from app.judge.starter_gates import StarterGate

_QUESTION_COLUMNS = "gate, key, version, type, instructions, criteria, note, active"
_GATE_COLUMNS = (
    "org_id, gate, version, enabled, model, fail_mode, allow_sensitive, "
    "max_state_chars, policy, note, active"
)


@dataclass(frozen=True)
class StoredQuestion:
    gate: str
    key: str
    version: int
    type: str
    instructions: JsonText
    criteria: Any
    note: str | None = None
    active: bool = False

    def question(self) -> Question:
        return Question(type=self.type, instructions=self.instructions, criteria=self.criteria)  # type: ignore[arg-type]


@dataclass(frozen=True)
class GateVersion:
    org_id: UUID
    gate: str
    version: int
    enabled: bool
    model: str
    fail_mode: FailMode
    allow_sensitive: bool
    max_state_chars: int
    policy: dict[str, Any]
    note: str | None = None
    active: bool = False


@dataclass(frozen=True)
class Gate:
    """A gate's live configuration: its active version and live questions."""

    config: GateVersion
    policy: Policy
    questions: dict[str, StoredQuestion]

    @property
    def name(self) -> str:
        return self.config.gate

    @property
    def version(self) -> int:
        return self.config.version

    def typed_questions(self) -> dict[str, Question]:
        return {key: stored.question() for key, stored in self.questions.items()}


def load_gate(connection: psycopg.Connection, *, org_id: UUID | str, gate: str) -> Gate:
    """The live version of a gate, checked for coherence.

    Read on every judgment, so a threshold change applies to the next call.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            f"select {_GATE_COLUMNS} from public.judge_gates "
            "where org_id = %s and gate = %s and active",
            (str(org_id), gate),
        )
        row = cursor.fetchone()
        if row is None:
            raise GateNotConfigured(gate)
        config = GateVersion(**row)
        cursor.execute(
            f"select {_QUESTION_COLUMNS} from public.judge_questions "
            "where org_id = %s and gate = %s and active order by key",
            (str(org_id), gate),
        )
        questions = {r["key"]: StoredQuestion(**r) for r in cursor.fetchall()}

    return _assemble(config, questions)


def _assemble(config: GateVersion, questions: dict[str, StoredQuestion]) -> Gate:
    try:
        policy = Policy.model_validate(config.policy)
    except ValidationError as error:
        raise GateMisconfigured(config.gate, [_first_line(error)]) from error
    if not questions:
        raise GateMisconfigured(config.gate, ["the gate has no live questions"])
    try:
        typed = {key: stored.question() for key, stored in questions.items()}
    except ValidationError as error:
        raise GateMisconfigured(config.gate, [_first_line(error)]) from error
    problems = policy.problems(typed)
    if problems:
        raise GateMisconfigured(config.gate, problems)
    return Gate(config=config, policy=policy, questions=questions)


def _first_line(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    return f"{where}: {first.get('msg')}" if where else str(first.get("msg"))


def _check_live(connection: psycopg.Connection, org_id: UUID | str, gate: str) -> None:
    """After a change, the gate as it now stands must still be coherent.

    Raised inside the writer's transaction, so an edit that would break the
    gate (a rule pointing at a retired question, say) is rolled back. A gate
    with no configuration yet is fine: questions usually come first.
    """
    try:
        load_gate(connection, org_id=org_id, gate=gate)
    except GateNotConfigured:
        return


def publish_question(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    gate: str,
    key: str,
    type: str,
    instructions: JsonText,
    criteria: dict[str, Any] | list[Any] | None = None,
    note: str | None = None,
) -> StoredQuestion:
    """Publish a new version of a question and make it live."""
    # The API's own shape, checked before it is stored.
    Question(type=type, instructions=instructions, criteria=criteria)  # type: ignore[arg-type]
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"select {_QUESTION_COLUMNS} from public.publish_judge_question"
            "(%s, %s, %s, %s, %s, %s, %s)",
            (
                str(org_id),
                gate,
                key,
                type,
                json.dumps(instructions),
                None if criteria is None else json.dumps(criteria),
                note,
            ),
        )
        stored = StoredQuestion(**cursor.fetchone())
        _check_live(conn, org_id, gate)
    return stored


def activate_question(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    gate: str,
    key: str,
    version: int | None,
) -> StoredQuestion | None:
    """Make an existing version live (rollback), or `version=None` to retire."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"select {_QUESTION_COLUMNS} from public.activate_judge_question(%s, %s, %s, %s)",
            (str(org_id), gate, key, version),
        )
        row = cursor.fetchone()
        _check_live(conn, org_id, gate)
    # A function returning a composite yields one all-null row for NULL.
    return StoredQuestion(**row) if row and row["key"] is not None else None


def publish_gate(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    gate: str,
    model: str,
    policy: dict[str, Any],
    fail_mode: FailMode = "closed",
    allow_sensitive: bool = False,
    enabled: bool = True,
    max_state_chars: int = 20000,
    note: str | None = None,
) -> GateVersion:
    """Publish a new version of a gate's configuration and make it live."""
    try:
        Policy.model_validate(policy)
    except ValidationError as error:
        raise GateMisconfigured(gate, [_first_line(error)]) from error
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"select {_GATE_COLUMNS} from public.publish_judge_gate"
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                str(org_id),
                gate,
                model,
                json.dumps(policy),
                fail_mode,
                allow_sensitive,
                enabled,
                max_state_chars,
                note,
            ),
        )
        version = GateVersion(**cursor.fetchone())
        _check_live(conn, org_id, gate)
    return version


def activate_gate(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    gate: str,
    version: int,
) -> GateVersion:
    """Make an existing gate version live: the one-step rollback."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"select {_GATE_COLUMNS} from public.activate_judge_gate(%s, %s, %s)",
            (str(org_id), gate, version),
        )
        result = GateVersion(**cursor.fetchone())
        _check_live(conn, org_id, gate)
    return result


def gate_history(
    connection: psycopg.Connection, *, user_id: UUID | str, org_id: UUID | str, gate: str
) -> tuple[list[GateVersion], list[StoredQuestion]]:
    """Every version of a gate and of its questions, newest first."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"select {_GATE_COLUMNS} from public.judge_gates "
            "where org_id = %s and gate = %s order by version desc",
            (str(org_id), gate),
        )
        versions = [GateVersion(**row) for row in cursor.fetchall()]
        cursor.execute(
            f"select {_QUESTION_COLUMNS} from public.judge_questions "
            "where org_id = %s and gate = %s order by key, version desc",
            (str(org_id), gate),
        )
        questions = [StoredQuestion(**row) for row in cursor.fetchall()]
    return versions, questions


def list_gates(
    connection: psycopg.Connection, *, user_id: UUID | str, org_id: UUID | str
) -> list[GateVersion]:
    """The live version of every gate in the org."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"select {_GATE_COLUMNS} from public.judge_gates "
            "where org_id = %s and active order by gate",
            (str(org_id),),
        )
        return [GateVersion(**row) for row in cursor.fetchall()]


def seed_gates(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    gates: Iterable[StarterGate],
) -> list[str]:
    """Publish starter gates the org has never had. Returns the gates seeded.

    A gate with any version already is left alone, whatever its state: the
    owner may have edited or disabled it on purpose.
    """
    seeded: list[str] = []
    for starter in gates:
        with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
            cursor.execute(
                "select 1 from public.judge_gates where org_id = %s and gate = %s limit 1",
                (str(org_id), starter.gate),
            )
            if cursor.fetchone() is not None:
                continue
        for question in starter.questions:
            publish_question(
                connection,
                user_id=user_id,
                org_id=org_id,
                gate=starter.gate,
                key=question.key,
                type=question.type,
                instructions=question.instructions,
                criteria=question.criteria,
                note=starter.note,
            )
        publish_gate(
            connection,
            user_id=user_id,
            org_id=org_id,
            gate=starter.gate,
            model=STARTER_MODEL,
            policy=starter.policy,
            fail_mode=starter.fail_mode,  # type: ignore[arg-type]
            note=starter.note,
        )
        seeded.append(starter.gate)
    return seeded
