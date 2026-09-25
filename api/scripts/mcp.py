"""MCP servers from the command line, until the Control Center has a screen (ADR 025).

    cd api
    uv run python -m scripts.mcp add higgsfield https://example.com/mcp [--auth oauth|bearer|none]
    uv run python -m scripts.mcp connect higgsfield     # oauth: prints the sign-in link
    uv run python -m scripts.mcp refresh higgsfield     # list its tools (new ones start off)
    uv run python -m scripts.mcp tools [higgsfield]     # read what each tool says it does
    uv run python -m scripts.mcp approve mcp_higgsfield_generate_image --risk R4 [--cap 10]
    uv run python -m scripts.mcp off mcp_higgsfield_generate_image
    uv run python -m scripts.mcp assign writer mcp_higgsfield_generate_image [--remove]
    uv run python -m scripts.mcp servers

A bearer token is read from the MCP_TOKEN environment variable, never from
the command line, and stored in Supabase Vault. The sign-in link sends your
browser back to PUBLIC_API_URL, so OAuth needs the deployed API.
"""

import argparse
import json
import os
import sys

from app.agents.admin import set_tools
from app.config import Settings
from app.db import connect
from app.mcp_servers import oauth
from app.mcp_servers.servers import (
    add_server,
    approve_tool,
    get_server,
    list_servers,
    list_tools,
    refresh,
    switch_off,
)
from scripts.agent import ROOT_ENV, _Env, _setup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("name")
    add.add_argument("url")
    add.add_argument("--auth", choices=["oauth", "bearer", "none"], default="oauth")
    for name in ("connect", "refresh"):
        commands.add_parser(name).add_argument("name")
    tools = commands.add_parser("tools")
    tools.add_argument("server", nargs="?")
    approve = commands.add_parser("approve")
    approve.add_argument("tool")
    approve.add_argument("--risk", choices=["R0", "R1", "R2", "R3", "R4"])
    approve.add_argument("--approval", choices=["auto", "approval"])
    approve.add_argument("--cap", type=int, help="most successful calls per day")
    commands.add_parser("off").add_argument("tool")
    assign = commands.add_parser("assign")
    assign.add_argument("agent")
    assign.add_argument("tool")
    assign.add_argument("--remove", action="store_true")
    commands.add_parser("servers")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, _ = _setup(connection)
        if args.command == "add":
            token = os.environ.get("MCP_TOKEN") if args.auth == "bearer" else None
            server = add_server(
                connection,
                user_id=user_id,
                org_id=org_id,
                name=args.name,
                url=args.url,
                auth=args.auth,
                token=token,
            )
            print(f"added {server['name']} ({server['auth']}); next: connect {server['name']}")
        elif args.command == "connect":
            server = get_server(connection, user_id=user_id, name=args.name)
            if server["auth"] == "oauth":
                if not settings.public_api_url:
                    raise SystemExit("Set PUBLIC_API_URL to the deployed API first")
                redirect = f"{settings.public_api_url.rstrip('/')}/mcp/oauth/callback"
                print("Open this link, sign in, and allow Pantheon:")
                print(
                    oauth.start(connection, user_id=user_id, server=server, redirect_uri=redirect)
                )
            else:
                print(
                    json.dumps(
                        refresh(connection, user_id=user_id, name=args.name).summary(), indent=2
                    )
                )
        elif args.command == "refresh":
            print(
                json.dumps(refresh(connection, user_id=user_id, name=args.name).summary(), indent=2)
            )
        elif args.command == "tools":
            for tool in list_tools(connection, user_id=user_id, server=args.server):
                state = "ON " if tool["enabled"] else ("CHANGED" if tool["changed"] else "off")
                print(
                    f"{state:7} {tool['name']}  (suggested {tool['suggested_risk']}, "
                    f"set {tool['risk_class']}, {tool['approval']})"
                )
                print(f"        {tool['description']}")
                print(f"        inputs: {json.dumps(tool['input_schema'].get('properties', {}))}")
        elif args.command == "approve":
            print(
                approve_tool(
                    connection,
                    user_id=user_id,
                    name=args.tool,
                    risk_class=args.risk,
                    approval=args.approval,
                    max_calls_per_day=args.cap,
                )
            )
        elif args.command == "off":
            switch_off(connection, user_id=user_id, name=args.tool)
            print(f"{args.tool} is off")
        elif args.command == "assign":
            print(
                set_tools(
                    connection,
                    user_id=user_id,
                    org_id=org_id,
                    name=args.agent,
                    add=[] if args.remove else [args.tool],
                    remove=[args.tool] if args.remove else [],
                )
            )
        else:
            for server in list_servers(connection, user_id=user_id):
                print(
                    f"{server['name']:14} {server['status']:10} {server['tools_on']}/"
                    f"{server['tools']} tools on  {server['url']}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
