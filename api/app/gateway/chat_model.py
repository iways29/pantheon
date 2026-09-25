"""A LangChain chat model over the gateway (Step 7.1, ADR 017).

LangGraph and deepagents expect a LangChain chat model. This one is the only
kind they are given: every call it makes is `Gateway.complete`, so the kill
switch, budgets, tier routing, cost logging and tracing apply to each turn of
an agent's loop exactly as to any other model call. No agent code ever holds
a provider client.
"""

import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict

from app.gateway.gateway import Gateway


class GatewayChatModel(BaseChatModel):
    """Calls models through the gateway on behalf of one agent (and run)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    gateway: Gateway
    agent_id: str
    run_id: str | None = None
    max_tokens: int | None = None
    sensitive: bool = False

    @property
    def _llm_type(self) -> str:
        return "pantheon-gateway"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,  # noqa: ANN401 - LangChain's signature
    ) -> Runnable[LanguageModelInput, AIMessage]:
        formatted = [convert_to_openai_tool(tool) for tool in tools]
        if tool_choice and tool_choice not in ("auto", "none", "any", "required"):
            tool_choice = {"type": "function", "function": {"name": tool_choice}}  # type: ignore[assignment]
        elif tool_choice in ("any", "required"):
            tool_choice = "required"
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,  # noqa: ANN401 - LangChain's signature
    ) -> ChatResult:
        response = self.gateway.complete(
            agent_id=self.agent_id,
            run_id=self.run_id,
            messages=[to_openai(message) for message in messages],
            max_tokens=self.max_tokens,
            sensitive=self.sensitive,
            tools=kwargs.get("tools"),
            tool_choice=kwargs.get("tool_choice"),
        )
        tool_calls, invalid = [], []
        for call in response.tool_calls:
            try:
                args = json.loads(call.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments are not an object")
            except ValueError as error:
                invalid.append(
                    {"name": call.name, "args": call.arguments, "id": call.id, "error": str(error)}
                )
                continue
            tool_calls.append({"name": call.name, "args": args, "id": call.id, "type": "tool_call"})
        message = AIMessage(
            content=response.text,
            tool_calls=tool_calls,
            invalid_tool_calls=invalid,
            response_metadata={
                "model_name": response.model,
                "finish_reason": response.finish_reason,
                "cost_usd": response.cost_usd,
            },
            usage_metadata={
                "input_tokens": response.tokens_in,
                "output_tokens": response.tokens_out,
                "total_tokens": response.tokens_in + response.tokens_out,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def to_openai(message: BaseMessage) -> dict[str, Any]:
    """A LangChain message in the OpenAI chat format OpenRouter takes."""
    content = _text(message.content)
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": content}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": content}
    if isinstance(message, ToolMessage):
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": content}
    if isinstance(message, AIMessage):
        out: dict[str, Any] = {"role": "assistant", "content": content or None}
        if message.tool_calls:
            out["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
                }
                for call in message.tool_calls
            ]
        return out
    return {"role": "user", "content": content}


def _text(content: str | list[Any]) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text", "")))
    return "".join(parts)
