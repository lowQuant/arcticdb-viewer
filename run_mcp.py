#!/usr/bin/env python3
import argparse

from core.connection import ConnectionManager, get_manager, load_connections

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="arcticdb-viewer MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport type (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host for SSE transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Port for SSE transport (default: 8001)",
    )
    parser.add_argument(
        "--connection",
        default=None,
        help="Name of a saved connection to use (overrides .env detection)",
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="Enable write tools (default: read-only, also controllable via ADBVIEW_READONLY)",
    )
    args = parser.parse_args()

    # Resolve read-only BEFORE importing the server, because write tools are
    # registered conditionally at import time.
    from core.settings import set_read_only, is_read_only
    if args.allow_writes:
        set_read_only(False)
    print(
        f"[arcticdb-viewer] MCP starting in "
        f"{'READ-ONLY' if is_read_only() else 'READ-WRITE'} mode."
    )

    uri: str | None = None
    name: str | None = None

    if args.connection:
        data = load_connections()
        match = next(
            (c for c in data["connections"] if c["name"] == args.connection), None
        )
        if not match:
            available = [c["name"] for c in data["connections"]]
            print(
                f"Error: saved connection {args.connection!r} not found. "
                f"Available: {available}"
            )
            raise SystemExit(1)
        uri, name = match["uri"], match["name"]
    else:
        env_conn = ConnectionManager.detect_env_connection()
        if env_conn:
            uri, name = env_conn["uri"], env_conn["name"]
        else:
            data = load_connections()
            saved = data["connections"]
            chosen = next(
                (c for c in saved if c["name"] == data.get("last_used")), None
            )
            if chosen is None and saved:
                chosen = saved[0]
            if chosen:
                uri, name = chosen["uri"], chosen["name"]

    if not uri:
        print(
            "Error: no ArcticDB connection available. "
            "Set AWS vars in .env, save a connection via the web UI, "
            "or pass --connection NAME."
        )
        raise SystemExit(1)

    get_manager().connect(uri, name=name)

    from mcp_server.server import run
    run(transport=args.transport, host=args.host, port=args.port)
