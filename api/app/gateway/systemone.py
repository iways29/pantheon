"""The HTTP boundary to TypeSafe's System One API (Jev).

Open decision 10, ADR 009: our own thin httpx client rather than the
`typesafe-sdk` package, in the style of `transport.py`, so there is no new
dependency and the kill switch, budgets and cost logging stay in the gateway.

Contract checked against the live API reference (docs.typesafe.ai/api.md,
read 2026-09-25):

    POST {base}/v1/systemone   Authorization: Bearer <key>
    body     {state, model, questions: {<key>: {type, instructions, criteria?}}}
    returns  {model, answers: {<key>: <answer>}, usage: {input_tokens, output_tokens}}

    noul    {"type": "noul", "noul": p}
    choice  {"type": "choice", "choice", "probabilities": {option: p}, "confidence"}
    score   {"type": "score", "score", "legend", "probabilities": {"0": p}, "confidence"}

Errors: 401 bad key, 422 invalid body, 429 rate limited, 529 overloaded. The
docs ask for exponential backoff on 429 and 529 and honour `retry-after`.
"""

import email.utils
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.gateway.errors import UpstreamError

TYPESAFE_BASE_URL = "https://api.typesafe.ai"
PROVIDER = "typesafe"

Probability = Annotated[float, Field(ge=0.0, le=1.0)]
JsonText = str | dict[str, Any] | list[Any]


# --- Request ---------------------------------------------------------------


class Question(BaseModel):
    """One typed question, exactly as the API takes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["noul", "choice", "score"]
    instructions: JsonText
    criteria: dict[str, Any] | list[Any] | None = None

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            body["criteria"] = self.criteria
        return body


# --- Response --------------------------------------------------------------


class _Answer(BaseModel):
    # Unknown fields are ignored so an additive API change does not turn every
    # judgment into a failure.
    model_config = ConfigDict(extra="ignore", frozen=True)


class NoulAnswer(_Answer):
    type: Literal["noul"]
    noul: Probability


class ChoiceAnswer(_Answer):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, Probability]
    confidence: Probability


class ScoreAnswer(_Answer):
    type: Literal["score"]
    score: float
    legend: dict[str, str]
    probabilities: dict[str, Probability]
    confidence: Probability


Answer = Annotated[NoulAnswer | ChoiceAnswer | ScoreAnswer, Field(discriminator="type")]


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    model: str
    answers: dict[str, Answer]
    usage: Usage


@dataclass(frozen=True)
class SystemOneResponse:
    #: The versioned model that answered, e.g. jev-1.13.0.
    model: str
    answers: dict[str, NoulAnswer | ChoiceAnswer | ScoreAnswer]
    tokens_in: int
    tokens_out: int
    latency_ms: int
    #: Filled in by the gateway from model_prices; the API reports no cost.
    cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def parse_response(body: object, *, latency_ms: int) -> SystemOneResponse:
    """Validate a response body against the documented contract."""
    try:
        parsed = _Body.model_validate(body)
    except ValidationError as error:
        failure = UpstreamError(
            f"TypeSafe answer did not match the contract: {error.errors()[:3]}",
            reason="malformed",
        )
        # A malformed answer may still have been billed. Carry what usage the
        # body reports so the gateway can cost it honestly.
        failure.billed = _billed_usage(body)
        raise failure from error
    return SystemOneResponse(
        model=parsed.model,
        answers=dict(parsed.answers),
        tokens_in=parsed.usage.input_tokens,
        tokens_out=parsed.usage.output_tokens,
        latency_ms=latency_ms,
        raw=body if isinstance(body, dict) else {},
    )


def _billed_usage(body: object) -> tuple[str, int, int] | None:
    if not isinstance(body, dict):
        return None
    try:
        usage = Usage.model_validate(body.get("usage"))
    except ValidationError:
        return None
    return str(body.get("model") or ""), usage.input_tokens, usage.output_tokens


# --- Transport -------------------------------------------------------------


class SystemOneTransport(Protocol):
    """Everything the gateway needs from TypeSafe."""

    def evaluate(
        self,
        *,
        model: str,
        state: JsonText,
        questions: dict[str, Question],
    ) -> SystemOneResponse: ...


class CircuitBreaker:
    """Stops calling a service that keeps failing, then tries again later.

    After `threshold` consecutive transient failures the circuit opens and
    calls fail at once, without a request, for `cooldown_seconds`. The next
    call after that is a trial: success closes the circuit, failure reopens
    it. Held by the transport, so it lives as long as a warm function instance
    and resets on a cold start, which is the right memory for an outage.
    """

    def __init__(
        self,
        *,
        threshold: int = 5,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        return self._clock() - self._opened_at < self._cooldown

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock()


#: Statuses worth another attempt: rate limited, overloaded, server faults.
_RETRYABLE = {429, 500, 502, 503, 504, 529}


class TypeSafeTransport:
    """Calls `POST /v1/systemone`.

    Retries are few and short on purpose. Jev answers in about 100 ms, and a
    serverless function has a hard time limit, so the whole call including
    waits is bounded by roughly `max_retries * max_wait_seconds` plus the
    timeouts. A gate that cannot get an answer applies its fail mode instead
    of waiting longer.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = TYPESAFE_BASE_URL,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        max_wait_seconds: float = 4.0,
        breaker: CircuitBreaker | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("TypeSafe API key is required")
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/v1/systemone"
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=3.0))
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._max_wait = max_wait_seconds
        self._breaker = breaker or CircuitBreaker()
        self._sleep = sleep

    def evaluate(
        self,
        *,
        model: str,
        state: JsonText,
        questions: dict[str, Question],
    ) -> SystemOneResponse:
        if self._breaker.is_open:
            raise UpstreamError(
                "TypeSafe calls are paused after repeated failures", reason="circuit_open"
            )

        payload = {
            "state": state,
            "model": model,
            "questions": {key: question.payload() for key, question in questions.items()},
        }
        started = time.monotonic()
        attempt = 0
        while True:
            try:
                response = self._client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
            except httpx.TimeoutException as error:
                failure = UpstreamError(f"TypeSafe timed out: {error}", reason="timeout")
                wait = None
            except httpx.HTTPError as error:
                failure = UpstreamError(f"TypeSafe unreachable: {error}", reason="unreachable")
                wait = None
            else:
                if response.status_code < 400:
                    self._breaker.record_success()
                    latency_ms = int((time.monotonic() - started) * 1000)
                    try:
                        body = response.json()
                    except ValueError as error:
                        raise UpstreamError(
                            "TypeSafe returned a body that is not JSON", reason="malformed"
                        ) from error
                    return parse_response(body, latency_ms=latency_ms)

                status = response.status_code
                message = f"TypeSafe returned {status}: {response.text[:300]}"
                if status not in _RETRYABLE:
                    # 401, 422 and friends: retrying will not help, and they
                    # say nothing about the service's health.
                    raise UpstreamError(message, status=status, reason="rejected")
                failure = UpstreamError(message, status=status, reason=_reason_for(status))
                wait = _retry_after_seconds(response.headers.get("retry-after"))

            if attempt >= self._max_retries:
                self._breaker.record_failure()
                raise failure
            delay = wait if wait is not None else self._backoff * (2**attempt)
            self._sleep(min(delay, self._max_wait))
            attempt += 1


def _reason_for(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status == 529:
        return "overloaded"
    return "server_error"


def _retry_after_seconds(value: str | None) -> float | None:
    """`retry-after` as seconds, from either form HTTP allows."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())
