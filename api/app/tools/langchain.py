"""The runtime's tools as LangChain tools, for LangGraph and deepagents.

Every call goes through `ToolRuntime.call`, so the allowlist, validation,
idempotency, approval holds, screening and logging apply to tool calls made
by a library loop exactly as to any other.
"""

from typing import Any

from langchain_core.tools import StructuredTool

from app.tools.registry import REGISTRY
from app.tools.runtime import ToolRuntime


def langchain_tools(
    runtime: ToolRuntime, offered: list[dict[str, Any]] | None = None
) -> list[StructuredTool]:
    tools = []
    for row in offered if offered is not None else runtime.allowed():
        spec = REGISTRY[row["name"]]

        def call(_name: str = spec.name, **kwargs: Any) -> str:  # noqa: ANN401
            return runtime.call(_name, kwargs).text

        tools.append(
            StructuredTool.from_function(
                func=call,
                name=spec.name,
                description=row["description"],
                args_schema=spec.args,
            )
        )
    return tools
