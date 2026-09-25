"""Running a tool call safely (Step 7.2, ADR 018).

Every call an agent makes goes through `ToolRuntime.call`, which, in order:

1. refuses an unknown tool, a tool switched off, or one the agent is not
   allowed (`agents.allowed_tools`);
2. validates the arguments against the tool's schema;
3. for a side-effecting tool, finds a call already made with the same
   idempotency key and returns its stored result instead of acting again; a
   call the owner has since approved runs now, once, with the approved
   arguments, and one the owner rejected returns the owner's note (Step 7.5);
4. holds for the owner any tool whose policy needs approval (always for R4),
   as a pending approval with a decision card, without running it;
5. applies the agent's autonomy level (ADR 022): the call runs, is held, or
   goes to the tool-risk gate, where clearly low-risk calls run, uncertain
   ones are held and clearly wrong ones are refused (ADR 021);
6. runs it, times it, and caps the size of what comes back;
7. for a tool that reads the outside world (R2), passes on text only if it
   was screened clean (Step 5.3);
8. records a `tool_calls` row and a `tool_called` event.

A refusal is returned to the model as an error it can read, not raised: the
agent should learn it may not do that. Kill switch and budget refusals from
inside a tool do propagate, and stop the run.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
from pydantic import ValidationError

from app.gateway import GatewayError
from app.tools.registry import REGISTRY, ToolSpec

#: The gate asked about R2 and R3 calls before they run (ADR 021).
RISK_GATE = "tool_risk"


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
    #: Talks to MCP servers (ADR 025); None: the default Streamable HTTP one.
    mcp: Any = None
    #: The judge (tool-risk gate, decision desk); None without TypeSafe.
    judge: Any = None
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
                select t.name, t.description, t.risk_class, t.approval, t.source,
                       t.input_schema
                from public.tools t
                join public.agents a on a.org_id = t.org_id
                where a.id = %s and t.enabled and t.name = any(a.allowed_tools)
                order by t.name
                """,
                (str(self._ctx.agent_id),),
            )
            return [
                dict(row)
                for row in cursor.fetchall()
                if row["source"] == "mcp" or row["name"] in REGISTRY
            ]

    def call(
        self,
        name: str,
        arguments: dict[str, Any] | str,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        config = self._config(name)
        spec = spec_for(name, config)
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

        key = idempotency_key or _derived_key(self._ctx.run_id, name, payload)
        earlier = self._earlier(key)
        if earlier is not None and (spec.side_effect or earlier["status"] != "ok"):
            return self._replay(spec, earlier)

        risk = config["risk_class"]
        if config["approval"] == "approval" or risk == "R4":
            return self._hold(
                name, payload, key, risk, "The tool always needs the owner's approval"
            )
        if config["max_calls_per_day"] and self._calls_today(name) >= config["max_calls_per_day"]:
            return self._finish(
                name,
                payload,
                None,
                "refused",
                {"error": f"{name} has reached its {config['max_calls_per_day']} calls for today"},
            )
        level, mode = self._mode(risk)
        if mode == "hold":
            return self._hold(
                name, payload, key, risk, f"An {level} agent needs approval for {risk} tools"
            )
        if mode == "gate":
            verdict, why = self._risk(spec, payload, risk)
            if verdict == "block":
                return self._finish(name, payload, None, "refused", {"error": why})
            if verdict == "ask":
                return self._hold(name, payload, key, risk, why)

        return self._run(spec, args, payload, key if spec.side_effect else None, config)

    def _run(
        self,
        spec: ToolSpec,
        args: Any,  # noqa: ANN401 - the tool's own argument model
        payload: dict[str, Any],
        key: str | None,
        config: dict[str, Any],
        *,
        replacing: UUID | None = None,
    ) -> ToolResult:
        name = spec.name
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
                replacing=replacing,
            )
        latency = _ms(started)
        if config["risk_class"] == "R2" and output.get("screened") != "clean":
            output = {k: v for k, v in output.items() if k not in ("text", "claims", "excerpt")} | {
                "note": "The content was not screened clean, so it is withheld"
            }
        overran = latency > config["timeout_seconds"] * 1000
        output = _capped(output, config["max_output_chars"])
        return self._finish(
            name,
            payload,
            key,
            "ok",
            output,
            latency_ms=latency,
            overran=overran,
            replacing=replacing,
        )

    def _replay(self, spec: ToolSpec, row: dict[str, Any]) -> ToolResult:
        """A call made before with the same key: the result, or the owner's decision."""
        status = row["status"]
        if status in ("ok", "held"):
            return ToolResult(row["tool"], status, row["result"] or {}, row["id"], True)
        if status == "rejected":
            return ToolResult(
                row["tool"],
                "refused",
                {"error": f"The owner rejected this call: {row['error'] or 'no reason given'}"},
                row["id"],
                True,
            )
        # Approved: run it now, once, with the arguments the owner approved.
        config = self._config(spec.name) or {}
        try:
            args = spec.args.model_validate(row["arguments"] or {})
        except ValidationError as error:
            return self._finish(
                spec.name,
                row["arguments"] or {},
                None,
                "error",
                {"error": f"The approved arguments are not valid: {error.errors()[:3]}"},
                replacing=row["id"],
            )
        return self._run(
            spec,
            args,
            args.model_dump(mode="json"),
            row["idempotency_key"],
            config,
            replacing=row["id"],
        )

    def _mode(self, risk: str) -> tuple[str, str]:
        """The agent's autonomy level, and what it means for this risk class:
        run, gate or hold (the ladder is data: `public.tool_mode`, ADR 022)."""
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                "select a.autonomy_level as level, "
                "public.tool_mode(a.org_id, a.autonomy_level, %s) as mode "
                "from public.agents a where a.id = %s",
                (risk, str(self._ctx.agent_id)),
            )
            row = cursor.fetchone()
        return (row["level"], row["mode"]) if row else ("L0", "hold")

    def _risk(self, spec: ToolSpec, payload: dict[str, Any], risk: str) -> tuple[str, str]:
        """The tool-risk gate's verdict: auto, ask or block, and why."""
        judge = self._ctx.judge
        if judge is None:
            # No TypeSafe: reads of the outside world run (they are screened
            # after), anything with an outside effect waits for a person.
            if risk in ("R0", "R2"):
                return "auto", ""
            return "ask", "No risk check is available, so a person must decide"
        decision = judge.run(
            RISK_GATE,
            {
                "tool": spec.name,
                "description": spec.description,
                "arguments": payload,
                "task": self._task_text(),
            },
            agent_id=self._ctx.agent_id,
            run_id=self._ctx.run_id,
            profile=risk.lower() if risk in ("R2", "R3") else None,
        )
        if decision.failed:
            return "ask", "The risk check gave no answer, so a person must decide"
        reasons = "; ".join(r.text for r in decision.reasons if r.outcome == decision.outcome)
        return decision.outcome, reasons or f"Risk check: {decision.outcome}"

    def _task_text(self) -> str:
        task_id = self._ctx.extras.get("task_id")
        if not task_id:
            return ""
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                "select title, instructions from public.tasks where id = %s", (str(task_id),)
            )
            row = cursor.fetchone()
        return f"{row['title']}. {row['instructions'] or ''}".strip() if row else ""

    # --- helpers -------------------------------------------------------------

    def _config(self, name: str) -> dict[str, Any] | None:
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                "select name, description, risk_class, approval, enabled, timeout_seconds, "
                "max_output_chars, source, mcp_server_id, remote_name, input_schema, "
                "max_calls_per_day from public.tools where org_id = %s and name = %s",
                (str(self._ctx.org_id), name),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def _calls_today(self, name: str) -> int:
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                "select count(*) as n from public.tool_calls where org_id = %s and tool = %s "
                "and status = 'ok' and created_at >= date_trunc('day', now())",
                (str(self._ctx.org_id), name),
            )
            return int(cursor.fetchone()["n"])

    def _granted(self, name: str) -> bool:
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                "select %s = any(allowed_tools) as ok from public.agents where id = %s",
                (name, str(self._ctx.agent_id)),
            )
            row = cursor.fetchone()
        return bool(row and row["ok"])

    def _earlier(self, key: str) -> dict[str, Any] | None:
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                "select id, tool, status, arguments, result, error, idempotency_key "
                "from public.tool_calls where org_id = %s and idempotency_key = %s "
                "and status in ('ok', 'held', 'approved', 'rejected')",
                (str(self._ctx.org_id), key),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def _hold(
        self, name: str, payload: dict[str, Any], key: str, risk: str, reason: str
    ) -> ToolResult:
        """Hold the call for the owner, with a decision card, and pause its task."""
        from app.approvals.desk import build_card

        action_key = f"tool:{name}"
        card = build_card(
            self._ctx,
            tool=name,
            arguments=payload,
            risk_class=risk,
            reason=reason,
            action_key=action_key,
        )
        approval_id = uuid4()
        held = self._finish(
            name,
            payload,
            key,
            "held",
            {
                "approval_id": str(approval_id),
                "message": "Held for the owner's approval",
                "reason": reason,
            },
        )
        task_id = self._ctx.extras.get("task_id")
        with self._ctx.connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.approvals
                    (id, org_id, run_id, action_type, payload, agent_output_snapshot,
                     idempotency_key, agent_id, task_id, tool_call_id, action_key,
                     recommendation, recommendation_probs, recommendation_request_id,
                     explanation, facts_checked, similar_decisions, conflicts)
                values (%s, %s, %s, 'tool_call', %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(approval_id),
                    str(self._ctx.org_id),
                    str(self._ctx.run_id) if self._ctx.run_id else None,
                    json.dumps(
                        {
                            "tool": name,
                            "arguments": payload,
                            "risk_class": risk,
                            "reason": reason,
                            "agent_id": str(self._ctx.agent_id),
                        }
                    ),
                    json.dumps({"tool": name, "arguments": payload}),
                    f"tool:{key}",
                    str(self._ctx.agent_id),
                    str(task_id) if task_id else None,
                    str(held.call_id),
                    action_key,
                    card.recommendation,
                    json.dumps(card.probabilities) if card.probabilities is not None else None,
                    card.request_id,
                    card.explanation,
                    json.dumps(card.facts_checked),
                    json.dumps(card.similar_decisions, default=str),
                    json.dumps(card.conflicts),
                ),
            )
            if task_id:
                cursor.execute(
                    "update public.tasks set status = 'awaiting_approval' "
                    "where id = %s and status = 'running'",
                    (str(task_id),),
                )
        return held

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
        replacing: UUID | None = None,
    ) -> ToolResult:
        """Record the call. `replacing` is an approved call now carried out:
        its row takes the outcome, so one key stays one row."""
        with self._ctx.connection.cursor() as cursor:
            if replacing is not None:
                cursor.execute(
                    """
                    update public.tool_calls
                       set status = %s, result = %s, error = %s, latency_ms = %s
                     where id = %s
                    returning id
                    """,
                    (
                        status,
                        json.dumps(output, default=str),
                        output.get("error") if status in ("refused", "error") else None,
                        latency_ms,
                        str(replacing),
                    ),
                )
            else:
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


def spec_for(name: str, config: dict[str, Any] | None = None) -> ToolSpec | None:
    """A built-in tool from code, or an MCP tool built from its `tools` row (ADR 025)."""
    if config is None or config.get("source", "builtin") == "builtin":
        return REGISTRY.get(name)
    from app.mcp_servers.definition import args_model
    from app.mcp_servers.servers import call

    return ToolSpec(
        name=name,
        description=config["description"],
        args=args_model(name, config["input_schema"] or {}),
        risk_class=config["risk_class"],
        # Anything that may change something acts at most once per key.
        side_effect=config["risk_class"] not in ("R0", "R2"),
        handler=lambda ctx, args: call(ctx, config, args.model_dump(mode="json")),
        timeout_seconds=config["timeout_seconds"],
        max_output_chars=config["max_output_chars"],
    )
