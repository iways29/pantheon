"""Talking to MCP servers, through the official SDK (ADR 025).

`McpGateway` is the only thing that opens an MCP connection. It resolves a
server's credential (bearer token, or an OAuth token refreshed as needed),
connects over Streamable HTTP, and lists or calls tools. Each call is its
own short connection: a serverless invocation holds nothing open.

The SDK is async; Pantheon's runtime is sync. Each call runs its own event
loop with `anyio.run`, which is safe in the worker threads LangGraph and
FastAPI call tools from.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import psycopg

from app.mcp_servers import credentials, oauth

#: Connect and read timeouts, in seconds. A tool's own timeout caps a call.
CONNECT_TIMEOUT = 15.0


@dataclass(frozen=True)
class RemoteTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] | None = None


@dataclass(frozen=True)
class RemoteResult:
    is_error: bool
    texts: list[str] = field(default_factory=list)
    structured: Any = None
    images: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)


#: Given a server's URL and headers, what `mcp.Client` should connect with.
#: Production: Streamable HTTP. Tests: an in-memory server object.
TransportFactory = Callable[[str, dict[str, str], float], Any]


def http_transport(url: str, headers: dict[str, str], timeout: float) -> Any:  # noqa: ANN401
    """Streamable HTTP with our headers, as the SDK documents it."""
    return _HttpTransport(url, headers, timeout)


class _HttpTransport:
    """An async context manager yielding the SDK's (read, write) streams."""

    def __init__(self, url: str, headers: dict[str, str], timeout: float) -> None:
        self._url, self._headers, self._timeout = url, headers, timeout

    async def __aenter__(self) -> Any:  # noqa: ANN401
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        self._http = httpx2.AsyncClient(
            headers=self._headers,
            timeout=httpx2.Timeout(CONNECT_TIMEOUT, read=self._timeout),
        )
        await self._http.__aenter__()
        self._transport = streamable_http_client(self._url, http_client=self._http)
        return await self._transport.__aenter__()

    async def __aexit__(self, *exc: object) -> None:
        try:
            await self._transport.__aexit__(*exc)
        finally:
            await self._http.__aexit__(*exc)


class McpGateway:
    def __init__(self, transport: TransportFactory = http_transport) -> None:
        self._transport = transport

    def list_tools(
        self, connection: psycopg.Connection, server: dict[str, Any]
    ) -> list[RemoteTool]:
        headers = self._headers(connection, server)
        return anyio.run(self._list, server["url"], headers)

    def call_tool(
        self,
        connection: psycopg.Connection,
        server: dict[str, Any],
        name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> RemoteResult:
        headers = self._headers(connection, server)
        return anyio.run(self._call, server["url"], headers, name, arguments, timeout)

    def _headers(self, connection: psycopg.Connection, server: dict[str, Any]) -> dict[str, str]:
        if server["auth"] == "none":
            return {}
        if server["auth"] == "bearer":
            token = credentials.get(connection, server_id=server["id"], kind="bearer")
            if not token:
                raise oauth.McpAuthRequired(f"No token stored for {server['name']}")
        else:
            token = oauth.access_token(connection, server=server)
        return {"authorization": f"Bearer {token}"}

    async def _list(self, url: str, headers: dict[str, str]) -> list[RemoteTool]:
        from mcp import Client

        tools: list[RemoteTool] = []
        async with Client(self._transport(url, headers, 60.0)) as client:
            cursor = None
            while True:
                page = await client.list_tools(cursor=cursor)
                for tool in page.tools:
                    annotations = (
                        tool.annotations.model_dump(by_alias=True, exclude_none=True)
                        if tool.annotations
                        else None
                    )
                    tools.append(
                        RemoteTool(
                            name=tool.name,
                            description=(tool.description or tool.title or tool.name)[:2000],
                            input_schema=dict(tool.input_schema or {"type": "object"}),
                            annotations=annotations,
                        )
                    )
                cursor = page.next_cursor
                if not cursor:
                    return tools

    async def _call(
        self,
        url: str,
        headers: dict[str, str],
        name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> RemoteResult:
        from mcp import Client
        from mcp.types import EmbeddedResource, ImageContent, ResourceLink, TextContent

        with anyio.fail_after(timeout + CONNECT_TIMEOUT):
            async with Client(self._transport(url, headers, timeout)) as client:
                result = await client.call_tool(name, arguments, read_timeout_seconds=timeout)
        texts, images, links = [], [], []
        for block in result.content:
            if isinstance(block, TextContent):
                texts.append(block.text)
            elif isinstance(block, ImageContent):
                # The image itself is not handed to a model: only that it exists.
                images.append({"mime_type": block.mime_type, "base64_chars": len(block.data)})
            elif isinstance(block, ResourceLink):
                links.append(
                    {"uri": str(block.uri), "name": block.name, "mime_type": block.mime_type}
                )
            elif isinstance(block, EmbeddedResource):
                links.append({"uri": str(block.resource.uri), "embedded": True})
        return RemoteResult(
            is_error=bool(result.is_error),
            texts=texts,
            structured=result.structured_content,
            images=images,
            links=links,
        )
