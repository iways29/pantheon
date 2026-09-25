"""Narrowing a long tool list with Jev (Step 7.2).

When an agent may call many tools, the model does better choosing among a
few. A Jev Choice over the tool descriptions (gate `tool_select`) ranks them
for the task, and the top few are offered. With few tools, or if Jev is
unsure or unreachable (the gate fails open), every allowed tool is offered:
narrowing is a convenience, never a restriction the agent cannot get past.
"""

from typing import Any
from uuid import UUID

from app.judge import Judge
from app.judge.store import load_gate
from app.tools.runtime import ToolRuntime


def offered_tools(
    runtime: ToolRuntime,
    judge: Judge,
    *,
    task: str,
    org_id: UUID | str,
    agent_id: UUID | str,
    run_id: UUID | str | None = None,
) -> list[dict[str, Any]]:
    tools = runtime.allowed()
    connection = runtime.context.connection
    settings = load_gate(connection, org_id=org_id, gate="tool_select").policy
    if len(tools) <= settings.setting("select_above", 6):
        return tools
    by_name = {t["name"]: t for t in tools}
    decision = judge.run(
        "tool_select",
        {"task": task, "tools": {t["name"]: t["description"] for t in tools}},
        agent_id=agent_id,
        run_id=run_id,
        extra_options={"tool": {t["name"]: t["description"] for t in tools}},
    )
    answer = decision.answers.get("tool")
    if decision.failed or decision.outcome == "unsure" or answer is None:
        return tools
    ranked = sorted(answer.probabilities.items(), key=lambda kv: kv[1], reverse=True)  # type: ignore[union-attr]
    top = [name for name, _ in ranked if name in by_name][
        : int(settings.setting("offer_at_most", 3))
    ]
    return [by_name[name] for name in top]
