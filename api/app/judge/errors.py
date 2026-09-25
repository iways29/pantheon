"""Refusals from the judge.

These are raised, not turned into an outcome: each means the judge was asked
something it must not answer, or is not set up to answer. The TypeSafe service
failing is different; that produces the gate's configured fail mode.
"""

from typing import Any


class JudgeError(RuntimeError):
    """Base for every refusal. `code` is stable; the message is not."""

    code = "judge_error"

    def __init__(self, message: str, *, gate: str) -> None:
        super().__init__(message)
        self.gate = gate

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "gate": self.gate, "message": str(self)}


class GateNotConfigured(JudgeError):
    code = "gate_not_configured"

    def __init__(self, gate: str) -> None:
        super().__init__(f"No active configuration for gate {gate!r}", gate=gate)


class GateMisconfigured(JudgeError):
    """The gate's policy and questions do not fit together.

    Raised rather than failing open or closed: a gate that cannot state its
    own rules is a configuration bug to fix, not an outage to ride out.
    """

    code = "gate_misconfigured"

    def __init__(self, gate: str, problems: list[str]) -> None:
        super().__init__(f"Gate {gate!r} is misconfigured: {'; '.join(problems)}", gate=gate)
        self.problems = problems

    def detail(self) -> dict[str, Any]:
        return {**super().detail(), "problems": self.problems}


class GateDisabled(JudgeError):
    """A disabled gate refuses to judge; it never waves things through."""

    code = "gate_disabled"

    def __init__(self, gate: str, version: int) -> None:
        super().__init__(f"Gate {gate!r} (version {version}) is disabled", gate=gate)


class SensitiveStateRefused(JudgeError):
    """Open decision 11: sensitive state does not go to TypeSafe."""

    code = "sensitive_state_refused"

    def __init__(self, gate: str) -> None:
        super().__init__(
            f"Gate {gate!r} does not allow sensitive state; route it to a person "
            "or a zero-retention model instead",
            gate=gate,
        )


class StateTooLarge(JudgeError):
    code = "state_too_large"

    def __init__(self, gate: str, size: int, limit: int) -> None:
        super().__init__(
            f"State for gate {gate!r} is {size} characters; the gate allows {limit}. "
            "Filter it in code first.",
            gate=gate,
        )
        self.size = size
        self.limit = limit

    def detail(self) -> dict[str, Any]:
        return {**super().detail(), "size": self.size, "limit": self.limit}
