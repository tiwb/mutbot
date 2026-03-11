"""mutbot.server — 轻量 ASGI server（h11 + wsproto）+ MCP 协议支持。

零 mutbot 内部依赖，接受任何 ASGI app。
"""

from __future__ import annotations

from mutbot.server._server import Server
from mutbot.server._mcp_server import MCPServer, mount_mcp
from mutbot.server._mcp_client import MCPClient, MCPError

__all__ = ["Server", "MCPServer", "MCPClient", "MCPError", "mount_mcp"]
