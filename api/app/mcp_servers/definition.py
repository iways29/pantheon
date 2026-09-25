"""What an MCP tool is, as Pantheon stores it (ADR 025).

A tool listed by a server becomes a `tools` row named mcp_<server>_<tool>,
with its definition fingerprinted: the owner approves one exact definition,
and a changed one switches the tool off (enforced in the database).
"""

import hashlib
import json
import re
from typing import Any

import jsonschema
from pydantic import BaseModel, ConfigDict, model_validator

_MAX_NAME = 64


def tool_name(server: str, remote: str) -> str:
    """mcp_<server>_<tool>, lower snake case, at most 64 characters."""
    slug = re.sub(r"[^a-z0-9]+", "_", remote.lower()).strip("_") or "tool"
    name = f"mcp_{server}_{slug}"
    if len(name) > _MAX_NAME:
        digest = hashlib.sha256(remote.encode()).hexdigest()[:8]
        name = f"{name[: _MAX_NAME - 9]}_{digest}"
    return name


def definition_sha(remote: str, description: str, input_schema: dict[str, Any]) -> str:
    body = json.dumps(
        {"name": remote, "description": description, "input_schema": input_schema},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def suggested_risk(annotations: dict[str, Any] | None) -> str:
    """A starting risk class from the server's hints (advice, never trusted).

    MCP's defaults are the cautious ones: a tool that says nothing is taken
    to write, to be destructive, and to reach the outside world, so R4.
    """
    hints = annotations or {}
    read_only = bool(hints.get("readOnlyHint", False))
    destructive = bool(hints.get("destructiveHint", True))
    open_world = bool(hints.get("openWorldHint", True))
    if read_only:
        return "R2" if open_world else "R0"
    if destructive:
        return "R4"
    return "R3" if open_world else "R1"


def args_model(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """A Pydantic model that accepts exactly what the tool's JSON Schema does."""
    validator = jsonschema.Draft202012Validator(schema or {"type": "object"})

    class McpArgs(BaseModel):
        model_config = ConfigDict(extra="allow", title=name)

        @model_validator(mode="before")
        @classmethod
        def _schema(cls, data: Any) -> Any:  # noqa: ANN401 - whatever the model sent
            errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
            if errors:
                first = errors[0]
                where = "/".join(str(p) for p in first.path) or "arguments"
                raise ValueError(f"{where}: {first.message}"[:300])
            return data

    return McpArgs
