"""Running a tool call safely (Step 7.2, ADR 018).

Every call an agent makes goes through `ToolRuntime.call`, which, in order:

1. refuses an unknown tool, a tool switched off, or one the agent is not
   allowed (`agents.allowed_tools`);
2. validates the arguments against the tool's schema;
3. for a side-effecting tool, finds a call already made with the same
   idempotency key and returns its stored result instead of acting again;
4. holds for the owner any tool whose policy needs approval (always for R4),
   as a pending approval, without running it;
5. runs it, times it, and caps the size of what comes back;
6. for a tool that reads the outside world (R2), passes on text only if it
   was screened clean (Step 5.3);
7. records a `tool_calls` row and a `tool_called` event.

A refusal is returned to the model as an error it can read, not raised: the
agent should learn it may not do that. Kill switch and budget refusals from
inside a tool do propagate, and stop the run.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg
from pydantic import ValidationError

from app.gateway import GatewayError
from app.tools.registry import REGISTRY, ToolSpec


@dataclass
class ToolContext:
    """What tool handlers may use. Built per agent session."""

    connection: psycopg.Connection
    org_id: UUID | str
    agent_id: UUID | str
    agent_name: str
    run_id: UUID | str | None = None
    #: Optional services; a tool whose service is missing reports an error.
    gateway: Any = None
    brain: Any = None
    writer: Any = None
    library: Any = None
    links: Any = None
    fetcher: Any = None
    tasks: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    tool: str
    status: str  # ok | refused | error | held
    output: dict[str, Any]
    call_id: UUID | None = None
    replayed: bool = False

    @property
    def text(self) -> str:
        """What the model reads back."""
        if self.status == "ok":
            return json.dumps(self.output, ensure_ascii=False, default=str)
        return json.dumps({"status": self.status, **self.output}, ensure_ascii=False, default=str)


class ToolRuntime:
    def __init__(self, context: ToolContext) -> None:
        import app.tools.builtin  # noqa: F401 - registers the built-in tools

        self._ctx = context

    @property
    def context(self) -> ToolContext:
        return self._ctx

    def allowed(self) -> list[dict[str, Any]]:
        """The tools this agent may call now, with the owner's descriptions."""
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                """
                select t.name, t.description, t.risk_class, t.approval
                from public.tools t
                join public.agents a on a.org_id = t.org_id
                where a.id = %s and t.enabled and t.name = any(a.allowed_tools)
                order by t.name
                """,
                (str(self._ctx.agent_id),),
            )
            return [dict(row) for row in cursor.fetchall() if row["name"] in REGISTRY]

    def call(
        self,
        name: str,
        arguments: dict[str, Any] | str,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        spec = REGISTRY.get(name)
        config = self._config(name)
        if spec is None or config is None:
            return self._finish(name, {}, None, "refused", {"error": f"No tool named {name!r}"})
        if not config["enabled"]:
            return self._finish(name, {}, None, "refused", {"error": f"{name} is switched off"})
        if not self._granted(name):
            return self._finish(
                name, {}, None, "refused", {"error": f"This agent may not call {name}"}
            )

        try:
            raw = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
            args = spec.args.model_validate(raw)
        except (ValueError, ValidationError) as error:
            detail = error.errors()[:3] if isinstance(error, ValidationError) else str(error)
            return self._finish(
                name, _safe(arguments), None, "refused", {"error": f"Bad arguments: {detail}"}
            )
        payload = args.model_dump(mode="json")

        key = None
        if spec.side_effect:
            key = idempotency_key or _derived_key(self._ctx.run_id, name, payload)
            earlier = self._earlier(key)
            if earlier is not None:
                return earlier

        if config["approval"] == "approval" or config["risk_class"] == "R4":
            approval_id = self._hold(name, payload, key)
            return self._finish(
                name,
                payload,
                key,
                "held",
                {"approval_id": str(approval_id), "message": "Held for the owner's approval"},
            )

        started = time.monotonic()
        try:
            output = spec.handler(self._ctx, args)
        except GatewayError:
            raise  # kill switch, budget: the run must stop
        except Exception as error:  # a failing tool is reported to the model
            return self._finish(
                name,
                payload,
                None,
                "error",
                {"error": f"{type(error).__name__}: {error}"[:500]},
                latency_ms=_ms(started),
            )
        latency = _ms(started)
        if config["risk_class"] == "R2" and output.get("screened") != "clean":
            output = {k: v for k, v in output.items() if k not in ("text", "claims", "excerpt")} | {
                "note": "The content was not screened clean, so it is withheld"
            }
        overran = latency > config["timeout_seconds"] * 1000
        output = _capped(output, config["max_output_chars"])
        return self._finish(name, payload, key, "ok", output, latency_ms=latency, overran=overran)

    # --- helpers -------------------------------------------------------------

    def _config(self, name: str) -> dict[str, Any] | None:
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                "select risk_class, approval, enabled, timeout_seconds, max_output_chars "
                "from public.tools where org_id = %s and name = %s",
                (str(self._ctx.org_id), name),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def _granted(self, name: str) -> bool:
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                "select %s = any(allowed_tools) as ok from public.agents where id = %s",
                (name, str(self._ctx.agent_id)),
            )
            row = cursor.fetchone()
        return bool(row and row["ok"])

    def _earlier(self, key: str) -> ToolResult | None:
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                "select id, tool, status, result from public.tool_calls "
                "where org_id = %s and idempotency_key = %s and status in ('ok', 'held')",
                (str(self._ctx.org_id), key),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ToolResult(row["tool"], row["status"], row["result"] or {}, row["id"], True)

    def _hold(self, name: str, payload: dict[str, Any], key: str | None) -> UUID:
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.approvals
                    (org_id, run_id, action_type, payload, agent_output_snapshot,
                     idempotency_key)
                values (%s, %s, 'tool_call', %s, %s, %s)
                on conflict (org_id, idempotency_key) where idempotency_key is not null
                do update set payload = excluded.payload
                returning id
                """,
                (
                    str(self._ctx.org_id),
                    str(self._ctx.run_id) if self._ctx.run_id else None,
                    json.dumps(
                        {"tool": name, "arguments": payload, "agent_id": str(self._ctx.agent_id)}
                    ),
                    json.dumps({"tool": name, "arguments": payload}),
                    f"tool:{key or _derived_key(self._ctx.run_id, name, payload)}",
                ),
            )
            return cursor.fetchone()["id"]

    def _finish(
        self,
        name: str,
        payload: dict[str, Any],
        key: str | None,
        status: str,
        output: dict[str, Any],
        *,
        latency_ms: int | None = None,
        overran: bool = False,
    ) -> ToolResult:
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.tool_calls
                    (org_id, agent_id, run_id, tool, idempotency_key, arguments, status,
                     result, error, latency_ms)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    str(self._ctx.org_id),
                    str(self._ctx.agent_id),
                    str(self._ctx.run_id) if self._ctx.run_id else None,
                    name,
                    key if status in ("ok", "held") else None,
                    json.dumps(payload, default=str),
                    status,
                    json.dumps(output, default=str),
                    output.get("error") if status in ("refused", "error") else None,
                    latency_ms,
                ),
            )
            call_id = cursor.fetchone()["id"]
            cursor.execute(
                "insert into public.events (org_id, run_id, agent_id, type, payload) "
                "values (%s, %s, %s, 'tool_called', %s)",
                (
                    str(self._ctx.org_id),
                    str(self._ctx.run_id) if self._ctx.run_id else None,
                    str(self._ctx.agent_id),
                    json.dumps(
                        {
                            "tool": name,
                            "status": status,
                            "call_id": str(call_id),
                            "latency_ms": latency_ms,
                            "overran": overran,
                        }
                    ),
                ),
            )
        return ToolResult(name, status, output, call_id)


def _derived_key(run_id: UUID | str | None, name: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, default=str)
    return f"{run_id or 'direct'}:{name}:{hashlib.sha256(body.encode()).hexdigest()[:24]}"


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _safe(arguments: object) -> dict[str, Any]:
    return arguments if isinstance(arguments, dict) else {"raw": str(arguments)[:500]}


def _capped(output: dict[str, Any], limit: int) -> dict[str, Any]:
    text = json.dumps(output, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return output
    return {"truncated": True, "output": text[: max(limit - 100, 0)]}


def spec_for(name: str) -> ToolSpec | None:
    return REGISTRY.get(name)
