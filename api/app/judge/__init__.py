"""The judge: TypeSafe Jev gates, configured in the database (ADR 009).

Callers use `Judge.run(gate, state, agent_id=...)` and act on the returned
`Decision`. Gates and their questions are data, managed through
`app.judge.store` (and `python -m scripts.judge` until the Control Center).
"""

import psycopg

from app.config import Settings
from app.gateway import gateway_from
from app.judge.errors import (
    GateDisabled,
    GateMisconfigured,
    GateNotConfigured,
    JudgeError,
    SensitiveStateRefused,
    StateTooLarge,
    UnknownProfile,
)
from app.judge.judge import Decision, Judge
from app.judge.policy import Policy, Reason, Rule


def judge_from(connection: psycopg.Connection, settings: Settings) -> Judge:
    """The real judge, wired from the environment. Fails without a TypeSafe key."""
    if not settings.typesafe_api_key:
        raise ValueError("TYPESAFE_API_KEY is not set; see .env.example")
    return Judge(connection, gateway_from(connection, settings))


__all__ = [
    "Decision",
    "GateDisabled",
    "GateMisconfigured",
    "GateNotConfigured",
    "Judge",
    "JudgeError",
    "Policy",
    "Reason",
    "Rule",
    "SensitiveStateRefused",
    "StateTooLarge",
    "UnknownProfile",
    "judge_from",
]
