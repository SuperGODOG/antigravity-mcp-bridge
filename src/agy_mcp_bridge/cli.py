"""
Command-line interface for running Antigravity MCP Bridge.
"""
import argparse
from agy_mcp_bridge.server import server


def main():
    parser = argparse.ArgumentParser(
        description="Antigravity MCP Bridge: Universal MCP daemon and orchestration bridge for Google Antigravity (agy)."
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport to use (default: stdio)."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for SSE transport (default: 8000)."
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host for SSE transport (default: 127.0.0.1)."
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        server.run(transport="stdio")
    elif args.transport == "sse":
        # Streamable HTTP / SSE mode
        server.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
