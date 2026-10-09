"""
Antigravity MCP Bridge: Universal MCP daemon and orchestration bridge for Google Antigravity (agy).
"""

__version__ = "0.1.0"

from agy_mcp_bridge.server import server

__all__ = ["server", "__version__"]
