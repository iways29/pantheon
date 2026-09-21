"""The OpenRouter request and response shape.

No network: httpx.MockTransport stands in for the endpoint so the request body
can be asserted directly. What this cannot prove is that OpenRouter still
accepts this shape -- only a live call does that, and it is unreachable from
the build environment. Treat these as a contract with the documented API, not
as confirmation the API matches the documentation.
"""

import json
from typing import Any

import httpx
import pytest

from app.gateway import SENSITIVE_PROVIDER_PREFERENCES, OpenRouterTransport, UpstreamError

SUCCESS_BODY: dict[str, Any] = {
    "model": "vendor/some-model",
    "provider": "SomeProvider",
    "choices": [{"message": {"role": "assistant", "content": "hello back"}}],
    "usage": {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "cost": 0.000123,
        "cost_details": {"upstream_inference_cost": 0.0001},
    },
}


def transport_returning(
    body: dict[str, Any], status: int = 200, sink: list[httpx.Request] | None = None
) -> OpenRouterTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if sink is not None:
            sink.append(request)
        return httpx.Response(status, json=body)

    return OpenRouterTransport(
        "test-key", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_a_successful_response_is_parsed_into_cost_and_tokens() -> None:
    result = transport_returning(SUCCESS_BODY).complete(
        model="vendor/some-model", messages=[{"role": "user", "content": "hi"}]
    )

    assert result.text == "hello back"
    assert result.tokens_in == 12
    assert result.tokens_out == 5
    assert result.cost_usd == pytest.approx(0.000123)
    assert result.provider == "SomeProvider"
    assert result.latency_ms >= 0


def test_the_request_carries_the_model_and_bearer_token() -> None:
    sink: list[httpx.Request] = []
    transport_returning(SUCCESS_BODY, sink=sink).complete(
        model="vendor/some-model", messages=[{"role": "user", "content": "hi"}], max_tokens=256
    )

    request = sink[0]
    body = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer test-key"
    assert body["model"] == "vendor/some-model"
    assert body["max_tokens"] == 256
    assert "provider" not in body


def test_sensitive_calls_send_the_provider_restriction() -> None:
    sink: list[httpx.Request] = []
    transport_returning(SUCCESS_BODY, sink=sink).complete(
        model="vendor/some-model",
        messages=[{"role": "user", "content": "hi"}],
        provider_preferences=SENSITIVE_PROVIDER_PREFERENCES,
    )

    body = json.loads(sink[0].content)
    assert body["provider"] == {"data_collection": "deny", "zdr": True}


def test_deprecated_usage_include_is_not_sent() -> None:
    """`usage: {include: true}` is deprecated and has no effect; usage is always returned."""
    sink: list[httpx.Request] = []
    transport_returning(SUCCESS_BODY, sink=sink).complete(
        model="vendor/some-model", messages=[{"role": "user", "content": "hi"}]
    )

    assert "usage" not in json.loads(sink[0].content)


def test_a_missing_cost_records_zero_rather_than_a_guess() -> None:
    body = {**SUCCESS_BODY, "usage": {"prompt_tokens": 3, "completion_tokens": 1}}

    result = transport_returning(body).complete(
        model="vendor/some-model", messages=[{"role": "user", "content": "hi"}]
    )

    assert result.cost_usd == 0.0
    assert result.tokens_in == 3


@pytest.mark.parametrize("status", [402, 429, 500])
def test_error_statuses_raise_upstream_error(status: int) -> None:
    transport = transport_returning({"error": {"message": "nope"}}, status=status)

    with pytest.raises(UpstreamError) as caught:
        transport.complete(model="vendor/some-model", messages=[{"role": "user", "content": "hi"}])

    assert caught.value.status == status


def test_an_empty_choices_list_is_an_error_not_an_empty_string() -> None:
    with pytest.raises(UpstreamError):
        transport_returning({"choices": []}).complete(
            model="vendor/some-model", messages=[{"role": "user", "content": "hi"}]
        )


def test_an_api_key_is_required() -> None:
    with pytest.raises(ValueError):
        OpenRouterTransport("")
