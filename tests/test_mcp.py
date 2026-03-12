"""MCP 协议测试

涵盖：
- JSON-RPC 2.0 分发器（单元测试）
- MCP server tool 自动发现（MCPToolSet Declaration）
- MCP client ↔ server 端到端（Streamable HTTP）
- Session 管理
- 错误处理
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mutagent.net.server import Server
from mutagent.net.mcp import MCPView, MCPToolSet
from mutagent.net.client import MCPClient, MCPError
from mutagent.net._mcp_impl import MCPToolProvider
from mutagent.net._mcp_proto import (
    JsonRpcDispatcher,
    JsonRpcError,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INTERNAL_ERROR,
    make_request,
    make_notification,
    ToolResult,
)


# ---------------------------------------------------------------------------
# JSON-RPC 分发器单元测试
# ---------------------------------------------------------------------------

class TestJsonRpcDispatcher:
    @pytest.fixture
    def dispatch(self) -> JsonRpcDispatcher:
        d = JsonRpcDispatcher()

        @d.method("echo")
        async def echo(params):
            return params

        @d.method("add")
        async def add(params):
            return {"sum": params["a"] + params["b"]}

        @d.method("fail")
        async def fail(params):
            raise JsonRpcError(-32000, "Custom error", {"detail": "test"})

        @d.method("crash")
        async def crash(params):
            raise RuntimeError("boom")

        @d.notification("log")
        async def on_log(params):
            pass  # notification，无返回

        return d

    @pytest.mark.asyncio
    async def test_method_call(self, dispatch):
        resp = await dispatch.handle({"jsonrpc": "2.0", "id": 1, "method": "echo", "params": {"x": 42}})
        assert resp == {"jsonrpc": "2.0", "id": 1, "result": {"x": 42}}

    @pytest.mark.asyncio
    async def test_method_with_params(self, dispatch):
        resp = await dispatch.handle({"jsonrpc": "2.0", "id": 2, "method": "add", "params": {"a": 3, "b": 4}})
        assert resp["result"]["sum"] == 7

    @pytest.mark.asyncio
    async def test_notification_returns_none(self, dispatch):
        resp = await dispatch.handle({"jsonrpc": "2.0", "method": "log", "params": {"msg": "hello"}})
        assert resp is None

    @pytest.mark.asyncio
    async def test_method_not_found(self, dispatch):
        resp = await dispatch.handle({"jsonrpc": "2.0", "id": 1, "method": "nonexistent"})
        assert resp["error"]["code"] == METHOD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_custom_error(self, dispatch):
        resp = await dispatch.handle({"jsonrpc": "2.0", "id": 1, "method": "fail"})
        assert resp["error"]["code"] == -32000
        assert resp["error"]["data"]["detail"] == "test"

    @pytest.mark.asyncio
    async def test_internal_error(self, dispatch):
        resp = await dispatch.handle({"jsonrpc": "2.0", "id": 1, "method": "crash"})
        assert resp["error"]["code"] == INTERNAL_ERROR

    @pytest.mark.asyncio
    async def test_missing_jsonrpc_version(self, dispatch):
        resp = await dispatch.handle({"id": 1, "method": "echo"})
        assert resp["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_parse_error(self, dispatch):
        result = await dispatch.handle_bytes(b"not json")
        data = json.loads(result)
        assert data["error"]["code"] == PARSE_ERROR

    @pytest.mark.asyncio
    async def test_batch_request(self, dispatch):
        batch = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "echo", "params": "a"},
            {"jsonrpc": "2.0", "id": 2, "method": "add", "params": {"a": 1, "b": 2}},
            {"jsonrpc": "2.0", "method": "log", "params": {}},  # notification
        ]).encode()
        result = await dispatch.handle_bytes(batch)
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) == 2  # 只有 2 个 response（notification 无响应）

    @pytest.mark.asyncio
    async def test_empty_batch(self, dispatch):
        result = await dispatch.handle_bytes(b"[]")
        data = json.loads(result)
        assert data["error"]["code"] == INVALID_REQUEST

    def test_make_request(self):
        msg = make_request(1, "test", {"key": "val"})
        assert msg == {"jsonrpc": "2.0", "id": 1, "method": "test", "params": {"key": "val"}}

    def test_make_notification(self):
        msg = make_notification("ping")
        assert msg == {"jsonrpc": "2.0", "method": "ping"}
        assert "id" not in msg


# ---------------------------------------------------------------------------
# MCPToolSet Declaration 测试
# ---------------------------------------------------------------------------

class _TestTools(MCPToolSet):
    """测试用 tool 集合。"""

    async def add(self, a: int, b: int) -> str:
        """Add two numbers"""
        return str(int(a) + int(b))

    async def fail_tool(self) -> str:
        """Always fails"""
        raise RuntimeError("intentional failure")


class TestMCPToolSetDiscovery:
    def test_tools_discovered(self):
        """MCPToolProvider 应自动发现 MCPToolSet 子类的方法。"""
        provider = MCPToolProvider()
        tools = provider.list_tools()
        names = [t["name"] for t in tools]
        assert "add" in names
        assert "fail_tool" in names

    def test_tool_schema(self):
        """Tool schema 应从函数签名推断。"""
        provider = MCPToolProvider()
        tools = provider.list_tools()
        add_tool = next(t for t in tools if t["name"] == "add")
        assert "a" in add_tool["inputSchema"]["properties"]
        assert "b" in add_tool["inputSchema"]["properties"]
        assert add_tool["description"] == "Add two numbers"

    @pytest.mark.asyncio
    async def test_call_tool(self):
        provider = MCPToolProvider()
        result = await provider.call_tool("add", {"a": 3, "b": 4})
        assert result.content[0]["text"] == "7"

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self):
        provider = MCPToolProvider()
        with pytest.raises(JsonRpcError):
            await provider.call_tool("nonexistent", {})


# ---------------------------------------------------------------------------
# MCP 端到端测试（Client ↔ Server over Streamable HTTP）
# ---------------------------------------------------------------------------

_MCP_PORT_BASE = 19200


class _TestMCPView(MCPView):
    """测试用 MCPView — Server 自动发现此子类。"""
    path = "/test-mcp"
    name = "test-server"
    version = "1.0.0"
    instructions = "A test server"


async def _start_mcp_server(port: int) -> Server:
    server = Server()
    await server.start(listen=[f"127.0.0.1:{port}"])
    return server


async def _stop_mcp_server(server: Server) -> None:
    await server.stop()


class TestMCPEndToEnd:
    @pytest.mark.asyncio
    async def test_initialize_and_list_tools(self):
        port = _MCP_PORT_BASE + 1
        server = await _start_mcp_server(port)
        try:
            client = MCPClient(url=f"http://127.0.0.1:{port}/test-mcp")
            await client.connect()
            try:
                assert client.server_info["name"] == "test-server"
                tools = await client.list_tools()
                names = [t["name"] for t in tools]
                assert "add" in names
                assert "fail_tool" in names
            finally:
                await client.close()
        finally:
            await _stop_mcp_server(server)

    @pytest.mark.asyncio
    async def test_call_tool(self):
        port = _MCP_PORT_BASE + 2
        server = await _start_mcp_server(port)
        try:
            client = MCPClient(url=f"http://127.0.0.1:{port}/test-mcp")
            await client.connect()
            try:
                result = await client.call_tool("add", a=3, b=4)
                assert result["content"][0]["text"] == "7"
                assert not result.get("isError", False)
            finally:
                await client.close()
        finally:
            await _stop_mcp_server(server)

    @pytest.mark.asyncio
    async def test_call_tool_error(self):
        port = _MCP_PORT_BASE + 3
        server = await _start_mcp_server(port)
        try:
            client = MCPClient(url=f"http://127.0.0.1:{port}/test-mcp")
            await client.connect()
            try:
                result = await client.call_tool("fail_tool")
                assert result.get("isError") is True
            finally:
                await client.close()
        finally:
            await _stop_mcp_server(server)

    @pytest.mark.asyncio
    async def test_ping(self):
        port = _MCP_PORT_BASE + 7
        server = await _start_mcp_server(port)
        try:
            client = MCPClient(url=f"http://127.0.0.1:{port}/test-mcp")
            await client.connect()
            try:
                await client.ping()  # 不抛异常即成功
            finally:
                await client.close()
        finally:
            await _stop_mcp_server(server)

    @pytest.mark.asyncio
    async def test_session_management(self):
        """初始化后应有 session ID。"""
        port = _MCP_PORT_BASE + 8
        server = await _start_mcp_server(port)
        try:
            client = MCPClient(url=f"http://127.0.0.1:{port}/test-mcp")
            await client.connect()
            try:
                from mutagent.net._client_impl import _ext
                ext = _ext(client)
                assert ext._session_id is not None
                assert len(ext._session_id) == 32  # hex(16 bytes)
            finally:
                await client.close()
        finally:
            await _stop_mcp_server(server)

    @pytest.mark.asyncio
    async def test_unknown_tool_error(self):
        port = _MCP_PORT_BASE + 9
        server = await _start_mcp_server(port)
        try:
            client = MCPClient(url=f"http://127.0.0.1:{port}/test-mcp")
            await client.connect()
            try:
                with pytest.raises(MCPError) as exc_info:
                    await client.call_tool("nonexistent")
                assert "Unknown tool" in str(exc_info.value)
            finally:
                await client.close()
        finally:
            await _stop_mcp_server(server)


# ---------------------------------------------------------------------------
# ToolResult 辅助方法测试
# ---------------------------------------------------------------------------

class TestToolResult:
    def test_text_result(self):
        r = ToolResult.text("hello")
        d = r.to_dict()
        assert d["content"][0]["type"] == "text"
        assert d["content"][0]["text"] == "hello"
        assert "isError" not in d

    def test_error_result(self):
        r = ToolResult.error("bad input")
        d = r.to_dict()
        assert d["isError"] is True
        assert d["content"][0]["text"] == "bad input"
