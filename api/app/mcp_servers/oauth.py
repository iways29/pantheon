"""Signing in to an MCP server, in two requests (ADR 025).

The MCP SDK's OAuth provider runs the whole authorization-code flow inside
one process, waiting for the browser to come back. A serverless API cannot
wait: the owner's click lands on a different invocation. So the flow is
split, following the MCP authorization spec and using the SDK's own models:

1. `start`: discover the server's authorization server (RFC 9728, then
   RFC 8414), register Pantheon as a client if needed (RFC 7591), make a
   PKCE pair and a state, store them as a pending sign-in (Vault, 10
   minutes), and return the URL for the owner's browser.
2. `finish`: the callback arrives with the code and the state; the state
   finds the pending sign-in (single use), the issuer is checked, the code
   is exchanged, and the tokens are stored in Vault.

After that `access_token` hands out a valid token, refreshing it when it is
about to expire. When refreshing is impossible the server is marked
`needs_auth` and the owner signs in again; an agent never sees any of it.
"""

import base64
import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
import psycopg
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

from app.db import acting_as
from app.mcp_servers import credentials

PENDING_MINUTES = 10
#: Refresh this long before the access token actually expires.
REFRESH_EARLY_SECONDS = 60


class McpAuthError(RuntimeError):
    status = 400


class McpAuthRequired(McpAuthError):
    """The owner must sign in to this server (again)."""

    status = 409


def start(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    server: dict[str, Any],
    redirect_uri: str,
    http: httpx.Client | None = None,
) -> str:
    """Begin a sign-in. Returns the URL to open in the owner's browser."""
    with http or _client() as client:
        url = server["url"]
        probe = client.post(
            url,
            json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
            headers={"accept": "application/json, text/event-stream"},
        )
        www_scope = extract_scope_from_www_auth(probe) if probe.status_code == 401 else None
        resource_url = (
            extract_resource_metadata_from_www_auth(probe)
            if probe.status_code in (401, 403)
            else None
        )
        prm = _first(
            client,
            build_protected_resource_metadata_discovery_urls(resource_url, url),
            ProtectedResourceMetadata,
        )
        auth_server = str(prm.authorization_servers[0]) if prm else None
        asm = _first(
            client,
            build_oauth_authorization_server_metadata_discovery_urls(auth_server, url),
            OAuthMetadata,
        )
        if asm is None:
            raise McpAuthError(f"{server['name']}: no OAuth authorization server was found")
        for endpoint in (asm.authorization_endpoint, asm.token_endpoint):
            if urlsplit(str(endpoint)).scheme != "https":
                raise McpAuthError("The authorization server must use https")

        stored = credentials.get_json(connection, server_id=server["id"], kind="oauth") or {}
        client_info = stored.get("client_info")
        if client_info is None or stored.get("redirect_uri") != redirect_uri:
            client_info = _register(client, asm, redirect_uri, server["name"])

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    state = secrets.token_urlsafe(32)
    scope = get_client_metadata_scopes(www_scope, prm, asm, ["authorization_code", "refresh_token"])
    params = {
        "response_type": "code",
        "client_id": client_info["client_id"],
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        # RFC 8707: the token is for this server and no other.
        "resource": str(prm.resource) if prm else url,
    }
    if scope:
        params["scope"] = scope
    credentials.put(
        connection,
        org_id=server["org_id"],
        server_id=server["id"],
        kind="oauth_pending",
        value={
            "verifier": verifier,
            "client_info": client_info,
            "metadata": asm.model_dump(mode="json", exclude_none=True),
            "resource": params["resource"],
            "redirect_uri": redirect_uri,
            "scope": scope,
            "user_id": str(user_id),
        },
        state=state,
        expires_at=datetime.now(UTC) + timedelta(minutes=PENDING_MINUTES),
    )
    _set_status(connection, user_id, server["id"], "needs_auth", None)
    return f"{asm.authorization_endpoint}?{urlencode(params)}"


def finish(
    connection: psycopg.Connection,
    *,
    state: str,
    code: str,
    iss: str | None = None,
    http: httpx.Client | None = None,
) -> dict[str, Any]:
    """Complete a sign-in from the callback. Returns the pending record's ids."""
    pending = credentials.pending_by_state(connection, state)
    if pending is None:
        raise McpAuthError("This sign-in link has expired or was already used; start again")
    credentials.drop(connection, server_id=pending["server_id"], kind="oauth_pending")
    asm = OAuthMetadata.model_validate(pending["metadata"])
    if iss is not None and iss.rstrip("/") != str(asm.issuer).rstrip("/"):
        raise McpAuthError("The sign-in came back from a different issuer; refused")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "code_verifier": pending["verifier"],
        "resource": pending["resource"],
    }
    with http or _client() as client:
        token = _token_request(client, asm, pending["client_info"], data)
    _store_token(connection, pending, token)
    _set_status(connection, pending["user_id"], pending["server_id"], "connected", None)
    return {
        "server_id": pending["server_id"],
        "org_id": pending["org_id"],
        "user_id": pending["user_id"],
    }


def access_token(
    connection: psycopg.Connection,
    *,
    server: dict[str, Any],
    http: httpx.Client | None = None,
) -> str:
    """A valid access token for the server, refreshed if needed."""
    stored = credentials.get_json(connection, server_id=server["id"], kind="oauth")
    if not stored or not stored.get("token"):
        raise McpAuthRequired(f"Sign in to {server['name']} first")
    token = OAuthToken.model_validate(stored["token"])
    expires_at = stored.get("expires_at")
    if expires_at is None or time.time() < expires_at - REFRESH_EARLY_SECONDS:
        return token.access_token
    if not token.refresh_token:
        _needs_auth(connection, server, "The sign-in expired")
    asm = OAuthMetadata.model_validate(stored["metadata"])
    data = {
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token,
        "resource": stored["resource"],
    }
    try:
        with http or _client() as client:
            fresh = _token_request(client, asm, stored["client_info"], data)
    except McpAuthError as error:
        _needs_auth(connection, server, str(error))
    if not fresh.refresh_token:
        fresh = fresh.model_copy(update={"refresh_token": token.refresh_token})
    _store_token(
        connection, {**stored, "server_id": server["id"], "org_id": server["org_id"]}, fresh
    )
    return fresh.access_token


# --- helpers --------------------------------------------------------------------------


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(15.0), follow_redirects=False)


def _first(client: httpx.Client, urls: list[str], model: type) -> Any:  # noqa: ANN401
    for url in urls:
        if urlsplit(url).scheme != "https":
            continue
        try:
            response = client.get(url, headers={"accept": "application/json"})
        except httpx.HTTPError:
            continue
        if response.status_code == 200:
            try:
                return model.model_validate(response.json())
            except ValueError:
                continue
    return None


def _register(
    client: httpx.Client, asm: OAuthMetadata, redirect_uri: str, server: str
) -> dict[str, Any]:
    if asm.registration_endpoint is None:
        raise McpAuthError(
            f"{server} does not let new apps register (no dynamic client registration); "
            "use a bearer token for it instead"
        )
    metadata = OAuthClientMetadata(
        client_name="Pantheon",
        redirect_uris=[redirect_uri],
        token_endpoint_auth_method="none",
    )
    response = client.post(
        str(asm.registration_endpoint),
        json=metadata.model_dump(mode="json", exclude_none=True),
    )
    if response.status_code not in (200, 201):
        raise McpAuthError(f"Registering with {server} failed ({response.status_code})")
    info = OAuthClientInformationFull.model_validate(response.json())
    return info.model_dump(mode="json", exclude_none=True)


def _token_request(
    client: httpx.Client, asm: OAuthMetadata, client_info: dict[str, Any], data: dict[str, str]
) -> OAuthToken:
    form = {**data, "client_id": client_info["client_id"]}
    auth = None
    secret = client_info.get("client_secret")
    if secret:
        if client_info.get("token_endpoint_auth_method") == "client_secret_post":
            form["client_secret"] = secret
        else:
            auth = (client_info["client_id"], secret)
    response = client.post(str(asm.token_endpoint), data=form, auth=auth)
    if response.status_code != 200:
        raise McpAuthError(f"The token request was refused ({response.status_code})")
    return OAuthToken.model_validate(response.json())


def _store_token(connection: psycopg.Connection, record: dict[str, Any], token: OAuthToken) -> None:
    expires_at = time.time() + token.expires_in if token.expires_in else None
    credentials.put(
        connection,
        org_id=record["org_id"],
        server_id=record["server_id"],
        kind="oauth",
        value={
            "token": token.model_dump(mode="json", exclude_none=True),
            "expires_at": expires_at,
            "client_info": record["client_info"],
            "metadata": record["metadata"],
            "resource": record["resource"],
            "redirect_uri": record.get("redirect_uri"),
        },
    )


def _needs_auth(connection: psycopg.Connection, server: dict[str, Any], why: str) -> None:
    from app.db import as_service_role

    with as_service_role(connection) as conn:
        conn.execute(
            "update public.mcp_servers set status = 'needs_auth', last_error = %s where id = %s",
            (why[:500], str(server["id"])),
        )
    raise McpAuthRequired(f"{server['name']}: {why}; the owner must sign in again")


def _set_status(
    connection: psycopg.Connection,
    user_id: UUID | str,
    server_id: UUID | str,
    status: str,
    error: str | None,
) -> None:
    with acting_as(connection, user_id=str(user_id)) as conn:
        conn.execute(
            "update public.mcp_servers set status = %s, last_error = %s where id = %s",
            (status, error, str(server_id)),
        )
