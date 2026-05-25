#!/usr/bin/env python3
import argparse

from core.connection import get_manager, ConnectionManager

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ADBView MCP Server")
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
    args = parser.parse_args()

    # Auto-connect from .env for MCP
    env_conn = ConnectionManager.detect_env_connection()
    if env_conn:
        get_manager().connect(env_conn["uri"], name=env_conn["name"])
    else:
        print("Error: No .env connection detected. Set AWS vars in .env.")
        raise SystemExit(1)

    from mcp_server.server import run
    run(transport=args.transport, host=args.host, port=args.port)
