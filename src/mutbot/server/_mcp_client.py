"""MCP client — 连接其他 MCP server（Streamable HTTP 传输）。"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from mutbot.server._mcp_types import PROTOCOL_VERSION

logger = logging.getLogger("mutbot.server.mcp_client")


class MCPClient:
    """MCP client — 通过 Streamable HTTP 连接 MCP server。

    用法::

        async with MCPClient("http://localhost:8000/mcp") as client:
            tools = await client.list_tools()
            result = await client.call_tool("search", query="hello")
    """

    def __init__(
        self,
        url: str,
        *,
        client_name: str = "mutbot",
        client_version: str = "0.1.0",
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.client_name = client_name
        self.client_version = client_version
        self.timeout = timeout

        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._request_id = 0
        self._server_info: dict[str, Any] = {}
        self._server_capabilities: dict[str, Any] = {}

    async def __aenter__(self) -> MCPClient:
        self._http = httpx.AsyncClient(timeout=self.timeout)
        await self._initialize()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        # 发送 DELETE 终止 session
        if self._http and self._session_id:
            try:
                await self._http.delete(
                    self.url,
                    headers={"Mcp-Session-Id": self._session_id},
                )
            except Exception:
                pass
        if self._http:
            await self._http.aclose()
            self._http = None

    # --- Public API ---

    @property
    def server_info(self) -> dict[str, Any]:
        return self._server_info

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self._server_capabilities

    async def list_tools(self) -> list[dict[str, Any]]:
        """获取 server 可用 tools。"""
        result = await self._request("tools/list")
        return result.get("tools", [])

    async def call_tool(self, name: str, **arguments: Any) -> dict[str, Any]:
        """调用 tool。"""
        result = await self._request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        return result

    async def list_resources(self) -> list[dict[str, Any]]:
        """获取 server 可用 resources。"""
        result = await self._request("resources/list")
        return result.get("resources", [])

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """读取 resource。"""
        result = await self._request("resources/read", {"uri": uri})
        return result

    async def list_prompts(self) -> list[dict[str, Any]]:
        """获取 server 可用 prompts。"""
        result = await self._request("prompts/list")
        return result.get("prompts", [])

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """获取 prompt。"""
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments
        result = await self._request("prompts/get", params)
        return result

    async def ping(self) -> None:
        """Ping server。"""
        await self._request("ping")

    # --- Internal ---

    async def _initialize(self) -> None:
        """MCP initialize 握手。"""
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": self.client_name,
                "version": self.client_version,
            },
        })

        self._server_info = result.get("serverInfo", {})
        self._server_capabilities = result.get("capabilities", {})
        logger.info("MCP initialized: %s v%s (protocol %s)",
                    self._server_info.get("name"),
                    self._server_info.get("version"),
                    result.get("protocolVersion"))

        # 发送 initialized notification
        await self._notify("notifications/initialized")

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _request(self, method: str, params: Any = None) -> Any:
        """发送 JSON-RPC request，返回 result。"""
        assert self._http is not None
        msg_id = self._next_id()
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        resp = await self._http.post(self.url, json=payload, headers=headers)
        resp.raise_for_status()

        # 检查 session ID
        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        content_type = resp.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            return self._parse_sse_response(resp.text, msg_id)
        else:
            data = resp.json()
            if "error" in data:
                raise MCPError(
                    data["error"].get("code", -1),
                    data["error"].get("message", "Unknown error"),
                    data["error"].get("data"),
                )
            return data.get("result")

    async def _notify(self, method: str, params: Any = None) -> None:
        """发送 JSON-RPC notification。"""
        assert self._http is not None
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        resp = await self._http.post(self.url, json=payload, headers=headers)
        # notification 应返回 202
        if resp.status_code not in (200, 202):
            logger.warning("Notification %s returned %d", method, resp.status_code)

    def _parse_sse_response(self, text: str, expected_id: int) -> Any:
        """解析 SSE 响应，提取 JSON-RPC result。"""
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                data_str = line[6:]
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # 可能是 batch
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("id") == expected_id:
                            if "error" in item:
                                err = item["error"]
                                raise MCPError(err.get("code", -1), err.get("message", ""), err.get("data"))
                            return item.get("result")
                elif isinstance(data, dict):
                    if "error" in data:
                        err = data["error"]
                        raise MCPError(err.get("code", -1), err.get("message", ""), err.get("data"))
                    return data.get("result")

        raise MCPError(-1, "No response found in SSE stream")


class MCPError(Exception):
    """MCP 协议错误。"""
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP error {code}: {message}")
