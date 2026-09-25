"""The HTTP boundary to OpenRouter.

Isolated behind a Protocol so budgets, the kill switch and cost logging are
testable without a network, and so swapping providers later touches one class.

Verified against OpenRouter's current documentation where this environment
could reach it; the notes below record what was confirmed and what was not.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.gateway.errors import UpstreamError

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: Provider preferences for work that must not be retained or trained on.
#: `data_collection: deny` skips providers that store or train on inputs, and
#: `zdr: true` requires zero-data-retention endpoints. Both are request-level,
#: so they hold regardless of account-wide dashboard defaults.
SENSITIVE_PROVIDER_PREFERENCES: dict[str, Any] = {"data_collection": "deny", "zdr": True}


@dataclass(frozen=True)
class ToolCall:
    """A function the model asked to call, as OpenRouter returns it."""

    id: str
    name: str
    #: The arguments as the model wrote them: a JSON string, not yet trusted.
    arguments: str

    def message(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class ModelResponse:
    model: str
    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    provider: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    tool_calls: tuple[ToolCall, ...] = ()
    #: `stop`, `tool_calls`, `length`, ... as the provider reported it.
    finish_reason: str | None = None

    def assistant_message(self) -> dict[str, Any]:
        """This reply as the assistant turn to send back with tool results."""
        message: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            message["tool_calls"] = [call.message() for call in self.tool_calls]
        return message


@dataclass(frozen=True)
class EmbeddingResponse:
    model: str
    vectors: list[list[float]]
    tokens_in: int
    cost_usd: float
    latency_ms: int
    provider: str | None = None
    #: The model the gateway asked for (the org's assignment). `model` is what
    #: the provider reports serving; the brain records this one, which is stable.
    requested_model: str | None = None


class EmbeddingTransport(Protocol):
    """What the gateway needs to turn text into vectors."""

    def embed(
        self,
        *,
        model: str,
        inputs: list[str],
        dimensions: int | None = None,
        provider_preferences: dict[str, Any] | None = None,
    ) -> EmbeddingResponse: ...


class Transport(Protocol):
    """Everything the gateway needs from a model provider."""

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        provider_preferences: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        plugins: list[dict[str, Any]] | None = None,
    ) -> ModelResponse: ...


class OpenRouterTransport:
    """Calls OpenRouter's chat completions endpoint.

    Note on cost: OpenRouter returns the authoritative per-request USD cost as
    `usage.cost`, so the gateway records what was actually charged rather than
    recomputing it from a local price table that would drift.

    `usage: {include: true}` is deliberately not sent. It is deprecated and has
    no effect; usage is always returned.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = OPENROUTER_BASE_URL,
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        provider_preferences: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        plugins: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """One chat completion. With `tools`, the reply may carry tool calls.

        Tool calling as OpenRouter documents it (read 2026-09-26): `tools` in
        OpenAI function format, resent on every turn; `tool_choice` `auto`,
        `none` or a named function; the reply's `finish_reason` is
        `tool_calls` and `message.tool_calls` holds id, name and JSON
        arguments; results go back as `tool` role messages. Tool calls are
        sequential (`parallel_tool_calls: false`) so every call is gated and
        logged one at a time.
        """
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            payload["parallel_tool_calls"] = False
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if provider_preferences:
            payload["provider"] = provider_preferences
        if plugins:
            payload["plugins"] = plugins

        started = time.monotonic()
        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.HTTPError as error:
            raise UpstreamError(f"OpenRouter request failed: {error}") from error
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise UpstreamError(
                f"OpenRouter returned {response.status_code}: {response.text[:500]}",
                status=response.status_code,
            )

        body = response.json()
        return _parse(body, model=model, latency_ms=latency_ms)

    def embed(
        self,
        *,
        model: str,
        inputs: list[str],
        dimensions: int | None = None,
        provider_preferences: dict[str, Any] | None = None,
    ) -> EmbeddingResponse:
        """`POST /embeddings` (OpenRouter API reference, read 2026-09-26).

        Body `{model, input: [...], dimensions?, encoding_format}`; the reply
        carries `data[].embedding` with `index`, and `usage.prompt_tokens`
        and `usage.cost`, OpenRouter's own charge, as for chat.
        """
        payload: dict[str, Any] = {"model": model, "input": inputs, "encoding_format": "float"}
        if dimensions is not None:
            payload["dimensions"] = dimensions
        if provider_preferences:
            payload["provider"] = provider_preferences

        started = time.monotonic()
        try:
            response = self._client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.HTTPError as error:
            raise UpstreamError(f"OpenRouter embeddings request failed: {error}") from error
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise UpstreamError(
                f"OpenRouter embeddings returned {response.status_code}: {response.text[:500]}",
                status=response.status_code,
            )
        return _parse_embeddings(
            response.json(), model=model, count=len(inputs), latency_ms=latency_ms
        )


def _parse_embeddings(
    body: dict[str, Any], *, model: str, count: int, latency_ms: int
) -> EmbeddingResponse:
    items = body.get("data") or []
    if len(items) != count:
        raise UpstreamError(
            f"OpenRouter returned {len(items)} embeddings for {count} inputs", reason="malformed"
        )
    ordered = sorted(items, key=lambda item: item.get("index", 0))
    vectors = []
    for item in ordered:
        vector = item.get("embedding")
        if not isinstance(vector, list):
            raise UpstreamError(
                "OpenRouter returned an embedding that is not a list", reason="malformed"
            )
        vectors.append([float(v) for v in vector])
    usage = body.get("usage") or {}
    return EmbeddingResponse(
        model=body.get("model") or model,
        vectors=vectors,
        tokens_in=int(usage.get("prompt_tokens") or 0),
        cost_usd=float(usage.get("cost") or 0.0),
        latency_ms=latency_ms,
        provider=body.get("provider"),
    )


def _parse(body: dict[str, Any], *, model: str, latency_ms: int) -> ModelResponse:
    choices = body.get("choices") or []
    if not choices:
        raise UpstreamError("OpenRouter returned no choices")

    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    usage = body.get("usage") or {}
    calls = []
    for raw_call in message.get("tool_calls") or []:
        function = raw_call.get("function") or {}
        if not function.get("name"):
            raise UpstreamError("OpenRouter returned a tool call with no name", reason="malformed")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {})
        calls.append(ToolCall(str(raw_call.get("id") or ""), function["name"], arguments))

    return ModelResponse(
        model=body.get("model") or model,
        provider=body.get("provider"),
        text=text,
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        # `cost` is OpenRouter's own figure for this request. Absent it, zero
        # is recorded rather than a guess, and the row still shows the tokens.
        cost_usd=float(usage.get("cost") or 0.0),
        latency_ms=latency_ms,
        raw=body,
        tool_calls=tuple(calls),
        finish_reason=choices[0].get("finish_reason"),
    )
