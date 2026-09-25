"""The TypeSafe transport against a scripted HTTP server.

The response bodies are the examples from TypeSafe's API reference
(docs.typesafe.ai/api.md, read 2026-09-25), so a parsing test is a test
against the documented contract, not against our own guess at it.
"""

import json
from collections.abc import Callable

import httpx
import pytest

from app.gateway import (
    ChoiceAnswer,
    CircuitBreaker,
    NoulAnswer,
    Question,
    ScoreAnswer,
    TypeSafeTransport,
    UpstreamError,
)

DOC_RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {
        "is_urgent": {"type": "noul", "noul": 0.95},
        "department": {
            "type": "choice",
            "choice": "billing",
            "probabilities": {"billing": 0.88, "technical": 0.12, "sales": 0.0},
            "confidence": 0.81,
        },
        "frustration": {
            "type": "score",
            "score": 1.05,
            "legend": {"0": "Calm", "1": "Frustrated", "2": "Very angry"},
            "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05},
            "confidence": 0.92,
        },
    },
    "usage": {"input_tokens": 318, "output_tokens": 34},
}

QUESTIONS = {
    "is_urgent": Question(type="noul", instructions="Does this convey urgency?"),
    "department": Question(
        type="choice",
        instructions="Which team should handle this?",
        criteria={"billing": "Payments", "technical": "Bugs", "sales": "Pricing"},
    ),
    "frustration": Question(
        type="score",
        instructions="How frustrated is the customer?",
        criteria=["Calm", "Frustrated", "Very angry"],
    ),
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleeps: list[float] | None = None,
    breaker: CircuitBreaker | None = None,
    max_retries: int = 2,
) -> TypeSafeTransport:
    record = sleeps if sleeps is not None else []
    return TypeSafeTransport(
        "ts-test-key",
        base_url="https://typesafe.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=record.append,
        breaker=breaker,
        max_retries=max_retries,
    )


def ask(transport: TypeSafeTransport) -> object:
    return transport.evaluate(
        model="jev-1.13.0",
        state="Help! My payouts have been failing for 3 days.",
        questions=QUESTIONS,
    )


def test_sends_the_documented_request_and_parses_every_answer_type() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DOC_RESPONSE)

    response = make(handler).evaluate(
        model="jev-1.13.0", state={"ticket": "payouts failing"}, questions=QUESTIONS
    )

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://typesafe.test/v1/systemone"
    assert request.headers["authorization"] == "Bearer ts-test-key"
    body = json.loads(request.content)
    assert body["model"] == "jev-1.13.0"
    assert body["state"] == {"ticket": "payouts failing"}
    assert body["questions"]["is_urgent"] == {
        "type": "noul",
        "instructions": "Does this convey urgency?",
    }, "a Noul with no criteria must not send a null criteria field"
    assert body["questions"]["frustration"]["criteria"] == ["Calm", "Frustrated", "Very angry"]

    assert response.model == "jev-1.13.0"
    assert response.tokens_in == 318 and response.tokens_out == 34
    assert isinstance(response.answers["is_urgent"], NoulAnswer)
    assert response.answers["is_urgent"].noul == 0.95
    choice = response.answers["department"]
    assert isinstance(choice, ChoiceAnswer) and choice.choice == "billing"
    assert choice.probabilities["technical"] == 0.12 and choice.confidence == 0.81
    score = response.answers["frustration"]
    assert isinstance(score, ScoreAnswer) and score.score == 1.05
    assert score.legend["2"] == "Very angry"


def test_unknown_extra_fields_are_ignored() -> None:
    body = json.loads(json.dumps(DOC_RESPONSE))
    body["request_id"] = "abc"
    body["answers"]["is_urgent"]["calibration"] = "new"

    response = ask(make(lambda _: httpx.Response(200, json=body)))

    assert response.answers["is_urgent"].noul == 0.95  # type: ignore[attr-defined, union-attr]


def test_retries_a_rate_limit_honouring_retry_after() -> None:
    replies = iter(
        [
            httpx.Response(429, headers={"retry-after": "2"}, text="slow down"),
            httpx.Response(200, json=DOC_RESPONSE),
        ]
    )
    sleeps: list[float] = []

    response = ask(make(lambda _: next(replies), sleeps=sleeps))

    assert response.model == "jev-1.13.0"
    assert sleeps == [2.0]


def test_backs_off_exponentially_when_overloaded_then_gives_up() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(529, text="overloaded")

    sleeps: list[float] = []
    with pytest.raises(UpstreamError) as caught:
        ask(make(handler, sleeps=sleeps))

    assert caught.value.reason == "overloaded"
    assert caught.value.status == 529
    assert len(calls) == 3, "one attempt plus two retries"
    assert sleeps == [0.5, 1.0]


def test_a_long_retry_after_is_capped() -> None:
    replies = iter(
        [
            httpx.Response(429, headers={"retry-after": "120"}),
            httpx.Response(200, json=DOC_RESPONSE),
        ]
    )
    sleeps: list[float] = []

    ask(make(lambda _: next(replies), sleeps=sleeps))

    assert sleeps == [4.0], "a serverless function cannot wait two minutes"


@pytest.mark.parametrize("status", [401, 422])
def test_a_refused_request_is_not_retried(status: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"detail": "no"})

    with pytest.raises(UpstreamError) as caught:
        ask(make(handler))

    assert caught.value.reason == "rejected"
    assert caught.value.status == status
    assert len(calls) == 1


def test_a_timeout_is_reported_as_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(UpstreamError) as caught:
        ask(make(handler, max_retries=0))

    assert caught.value.reason == "timeout"


def test_a_connection_failure_is_reported_as_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(UpstreamError) as caught:
        ask(make(handler, max_retries=0))

    assert caught.value.reason == "unreachable"


def test_a_body_off_contract_is_malformed_and_keeps_the_billed_usage() -> None:
    body = {
        "model": "jev-1.13.0",
        "answers": {"is_urgent": {"type": "noul", "noul": 1.7}},
        "usage": {"input_tokens": 300, "output_tokens": 5},
    }

    with pytest.raises(UpstreamError) as caught:
        ask(make(lambda _: httpx.Response(200, json=body)))

    assert caught.value.reason == "malformed"
    assert caught.value.billed == ("jev-1.13.0", 300, 5)


def test_a_body_that_is_not_json_is_malformed() -> None:
    with pytest.raises(UpstreamError) as caught:
        ask(make(lambda _: httpx.Response(200, text="<html>gateway</html>")))

    assert caught.value.reason == "malformed"


def test_the_circuit_opens_after_repeated_failures_and_stops_calling() -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=30, clock=clock)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    transport = make(handler, breaker=breaker, max_retries=0)
    for _ in range(2):
        with pytest.raises(UpstreamError):
            ask(transport)
    assert len(calls) == 2

    with pytest.raises(UpstreamError) as caught:
        ask(transport)
    assert caught.value.reason == "circuit_open"
    assert len(calls) == 2, "an open circuit must not send a request"


def test_the_circuit_tries_again_after_the_cooldown_and_closes_on_success() -> None:
    clock = Clock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=30, clock=clock)
    replies = iter([httpx.Response(503), httpx.Response(200, json=DOC_RESPONSE)])
    transport = make(lambda _: next(replies), breaker=breaker, max_retries=0)

    with pytest.raises(UpstreamError):
        ask(transport)
    assert breaker.is_open

    clock.now += 31
    assert not breaker.is_open
    ask(transport)
    assert not breaker.is_open


def test_refusals_do_not_trip_the_circuit() -> None:
    """A 422 is our mistake, not TypeSafe's outage."""
    breaker = CircuitBreaker(threshold=1)
    transport = make(lambda _: httpx.Response(422), breaker=breaker)

    with pytest.raises(UpstreamError):
        ask(transport)

    assert not breaker.is_open


def test_an_api_key_is_required() -> None:
    with pytest.raises(ValueError):
        TypeSafeTransport("")
