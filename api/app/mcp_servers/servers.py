"""MCP servers and their tools, for the owner and for agents (ADR 025).

Owner side: add a server, sign in, list its tools into `tools`, read and
approve a tool (setting its risk class), switch one off. Agent side: `call`,
used by the tool runtime for a tool whose source is `mcp`, after every
check the runtime makes for any tool.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

import psycopg

from app.db import acting_as
from app.mcp_servers import credentials
from app.mcp_servers.client import McpGateway
from app.mcp_servers.definition import definition_sha, suggested_risk, tool_name
from app.mcp_servers.oauth import McpAuthRequired

Auth = Literal["none", "bearer", "oauth"]


class McpError(ValueError):
    status = 400


class McpNotFound(McpError):
    status = 404


@dataclass
class ListReport:
    server: str
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "added": self.added,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "removed": self.removed,
        }


def add_server(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    name: str,
    url: str,
    auth: Auth = "oauth",
    token: str | None = None,
) -> dict[str, Any]:
    if auth == "bearer" and not token:
        raise McpError("A bearer server needs its token")
    if auth != "bearer" and token:
        raise McpError("Only a bearer server takes a token")
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        try:
            cursor.execute(
                "insert into public.mcp_servers (org_id, name, url, auth, status) "
                "values (%s, %s, %s, %s, %s) returning *",
                (str(org_id), name, url, auth, "needs_auth" if auth == "oauth" else "new"),
            )
        except psycopg.errors.UniqueViolation as error:
            raise McpError(f"A server named {name!r} already exists") from error
        except psycopg.errors.CheckViolation as error:
            raise McpError(
                "The name must be 2 to 21 lowercase letters or digits, and the URL https"
            ) from error
        server = dict(cursor.fetchone())
    if token:
        credentials.put(
            connection, org_id=org_id, server_id=server["id"], kind="bearer", value=token
        )
    return server


def get_server(connection: psycopg.Connection, *, user_id: UUID | str, name: str) -> dict[str, Any]:
    with acting_as(connection, user_id=str(user_id)) as conn:
        row = conn.execute("select * from public.mcp_servers where name = %s", (name,)).fetchone()
    if row is None:
        raise McpNotFound(f"No MCP server {name!r}")
    return dict(row)


def list_servers(connection: psycopg.Connection, *, user_id: UUID | str) -> list[dict[str, Any]]:
    with acting_as(connection, user_id=str(user_id)) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "select s.id, s.name, s.url, s.auth, s.status, s.last_error, s.enabled, "
                "s.last_listed_at, count(t.id) as tools, "
                "count(t.id) filter (where t.enabled) as tools_on "
                "from public.mcp_servers s left join public.tools t on t.mcp_server_id = s.id "
                "group by s.id order by s.name"
            ).fetchall()
        ]


def list_tools(
    connection: psycopg.Connection, *, user_id: UUID | str, server: str | None = None
) -> list[dict[str, Any]]:
    """MCP tools as the owner reviews them: what the server says, and our state."""
    with acting_as(connection, user_id=str(user_id)) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "select t.name, s.name as server, t.remote_name, t.description, t.input_schema, "
                "t.annotations, t.suggested_risk, t.risk_class, t.approval, t.enabled, "
                "t.max_calls_per_day, t.approved_at, "
                "(t.approved_sha is not null and t.approved_sha <> t.definition_sha) as changed "
                "from public.tools t join public.mcp_servers s on s.id = t.mcp_server_id "
                "where (%s::text is null or s.name = %s) order by s.name, t.name",
                (server, server),
            ).fetchall()
        ]


def refresh(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    name: str,
    gateway: McpGateway | None = None,
) -> ListReport:
    """List the server's tools into `tools`. New ones start off; changed ones switch off."""
    server = get_server(connection, user_id=user_id, name=name)
    try:
        remote = (gateway or McpGateway()).list_tools(connection, server)
    except McpAuthRequired:
        raise
    except Exception as error:  # recorded for the owner, then raised
        _status(connection, user_id, server["id"], "error", f"{type(error).__name__}: {error}")
        raise McpError(f"Could not list tools on {name}: {error}") from error

    report = ListReport(name)
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select name, definition_sha from public.tools where mcp_server_id = %s",
            (str(server["id"]),),
        )
        existing = {r["name"]: r["definition_sha"] for r in cursor.fetchall()}
        seen = set()
        for tool in remote:
            local = tool_name(name, tool.name)
            seen.add(local)
            sha = definition_sha(tool.name, tool.description, tool.input_schema)
            risk = suggested_risk(tool.annotations)
            if local not in existing:
                cursor.execute(
                    """
                    insert into public.tools
                        (org_id, name, description, risk_class, approval, enabled, source,
                         mcp_server_id, remote_name, input_schema, annotations, suggested_risk,
                         definition_sha, timeout_seconds, max_output_chars)
                    values (%s, %s, %s, %s, %s, false, 'mcp', %s, %s, %s, %s, %s, %s, 45, 8000)
                    """,
                    (
                        server["org_id"],
                        local,
                        tool.description,
                        risk,
                        "approval" if risk == "R4" else "auto",
                        str(server["id"]),
                        tool.name,
                        json.dumps(tool.input_schema),
                        json.dumps(tool.annotations),
                        risk,
                        sha,
                    ),
                )
                report.added.append(local)
            elif existing[local] != sha:
                # The database switches it off unless this is the approved one.
                cursor.execute(
                    """
                    update public.tools
                       set description = %s, input_schema = %s, annotations = %s,
                           suggested_risk = %s, definition_sha = %s, remote_name = %s
                     where mcp_server_id = %s and name = %s
                    """,
                    (
                        tool.description,
                        json.dumps(tool.input_schema),
                        json.dumps(tool.annotations),
                        risk,
                        sha,
                        tool.name,
                        str(server["id"]),
                        local,
                    ),
                )
                report.changed.append(local)
            else:
                report.unchanged.append(local)
        gone = sorted(set(existing) - seen)
        if gone:
            cursor.execute(
                "update public.tools set enabled = false where mcp_server_id = %s "
                "and name = any(%s)",
                (str(server["id"]), gone),
            )
            report.removed = gone
        cursor.execute(
            "update public.mcp_servers set status = 'connected', last_error = null, "
            "last_listed_at = now() where id = %s",
            (str(server["id"]),),
        )
        cursor.execute(
            "insert into public.events (org_id, type, payload) values (%s, 'mcp_tools_listed', %s)",
            (str(server["org_id"]), json.dumps(report.summary())),
        )
    return report


def approve_tool(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    name: str,
    risk_class: str | None = None,
    approval: Literal["auto", "approval"] | None = None,
    max_calls_per_day: int | None = None,
) -> dict[str, Any]:
    """The owner has read this exact definition and switches it on."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select name, risk_class, approval, suggested_risk from public.tools "
            "where name = %s and source = 'mcp'",
            (name,),
        )
        row = cursor.fetchone()
        if row is None:
            raise McpNotFound(f"No MCP tool {name!r}")
        risk = risk_class or row["risk_class"]
        mode = "approval" if risk == "R4" else (approval or row["approval"])
        cursor.execute(
            """
            update public.tools
               set risk_class = %s, approval = %s, max_calls_per_day = %s,
                   approved_sha = definition_sha, approved_at = now(), approved_by = %s,
                   enabled = true
             where name = %s
            returning name, risk_class, approval, enabled, max_calls_per_day
            """,
            (risk, mode, max_calls_per_day, str(user_id), name),
        )
        return dict(cursor.fetchone())


def switch_off(connection: psycopg.Connection, *, user_id: UUID | str, name: str) -> None:
    with acting_as(connection, user_id=str(user_id)) as conn:
        cursor = conn.execute(
            "update public.tools set enabled = false where name = %s and source = 'mcp'", (name,)
        )
        if cursor.rowcount == 0:
            raise McpNotFound(f"No MCP tool {name!r}")


# --- The agent side ----------------------------------------------------------------------


def call(ctx: Any, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
    """Run an approved MCP tool for an agent; screen what comes back."""
    with ctx.connection.cursor() as cursor:
        cursor.execute(
            "select * from public.mcp_servers where id = %s", (str(config["mcp_server_id"]),)
        )
        server = cursor.fetchone()
    if server is None or not server["enabled"]:
        raise RuntimeError("That tool's server is switched off")
    gateway = ctx.mcp or McpGateway()
    result = gateway.call_tool(
        ctx.connection,
        dict(server),
        config["remote_name"],
        payload,
        float(config["timeout_seconds"]),
    )
    text = "\n".join(result.texts)
    if not text and result.structured is not None:
        text = json.dumps(result.structured, ensure_ascii=False, default=str)
    if result.is_error:
        raise RuntimeError(f"The tool reported an error: {text[:300]}")
    output: dict[str, Any] = {}
    if result.images:
        output["images"] = result.images
    if result.links:
        output["links"] = result.links
    if not text:
        return output | {"screened": "clean"}
    if ctx.judge is None:
        return output | {
            "screened": "unscreened",
            "note": "The tool's text is withheld: it cannot be screened here",
        }
    from app.judge.screening import Screener

    screening = Screener(ctx.connection, ctx.judge, ctx.brain).screen(
        text,
        purpose=f"The output of the tool {config['name']}",
        source_kind="message",
        org_id=ctx.org_id,
        agent_id=ctx.agent_id,
        source_ref=f"mcp:{server['name']}:{config['remote_name']}",
        run_id=ctx.run_id,
    )
    admitted = screening.admitted_text()
    if admitted is None:
        return output | {"screened": screening.label, "reasons": list(screening.reasons)}
    return output | {"screened": "clean", "text": admitted}


def _status(
    connection: psycopg.Connection, user_id: UUID | str, server_id: UUID, status: str, error: str
) -> None:
    with acting_as(connection, user_id=str(user_id)) as conn:
        conn.execute(
            "update public.mcp_servers set status = %s, last_error = %s where id = %s",
            (status, error[:500], str(server_id)),
        )
