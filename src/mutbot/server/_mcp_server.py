"""MCP server 框架 — tool/resource/prompt 注册 + Streamable HTTP 端点。"""

from __future__ import annotations

import inspect
import json
import logging
import secrets
from typing import Any, Awaitable, Callable

from mutbot.server._jsonrpc import JsonRpcDispatcher, JsonRpcError, INVALID_PARAMS
from mutbot.server._mcp_types import (
    PROTOCOL_VERSION,
    PromptDef,
    ResourceDef,
    ServerCapabilities,
    ToolDef,
    ToolResult,
)
from mutbot.server._sse import format_sse

logger = logging.getLogger("mutbot.server.mcp")

ToolHandler = Callable[..., Awaitable[ToolResult | str]]
ResourceHandler = Callable[..., Awaitable[Any]]
PromptHandler = Callable[..., Awaitable[list[dict[str, Any]]]]


class MCPServer:
    """MCP server — 注册 tools/resources/prompts 并通过 Streamable HTTP 提供服务。

    用法::

        mcp = MCPServer(name="my-server", version="1.0.0")

        @mcp.tool(description="Search the web")
        async def search_web(query: str) -> str:
            return "results..."

        # 挂载到 ASGI app
        app = mount_mcp(app, "/mcp", mcp)
    """

    def __init__(
        self,
        name: str = "mutbot",
        version: str = "0.1.0",
        *,
        instructions: str | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.instructions = instructions

        self._tools: dict[str, tuple[ToolDef, ToolHandler]] = {}
        self._resources: dict[str, tuple[ResourceDef, ResourceHandler]] = {}
        self._prompts: dict[str, tuple[PromptDef, PromptHandler]] = {}

        # 活跃 session
        self._sessions: dict[str, _MCPSession] = {}

        # JSON-RPC 分发器
        self._dispatch = JsonRpcDispatcher()
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        """注册 MCP JSON-RPC 方法。"""
        self._dispatch.add_method("initialize", self._handle_initialize)
        self._dispatch.add_notification("notifications/initialized", self._handle_initialized)
        self._dispatch.add_method("ping", self._handle_ping)
        self._dispatch.add_method("tools/list", self._handle_tools_list)
        self._dispatch.add_method("tools/call", self._handle_tools_call)
        self._dispatch.add_method("resources/list", self._handle_resources_list)
        self._dispatch.add_method("resources/read", self._handle_resources_read)
        self._dispatch.add_method("prompts/list", self._handle_prompts_list)
        self._dispatch.add_method("prompts/get", self._handle_prompts_get)

    # --- 装饰器 API ---

    def tool(
        self,
        name: str | None = None,
        *,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """注册 MCP tool（装饰器）。

        handler 返回 str 自动包装为 ToolResult.text()。
        """
        def decorator(fn: ToolHandler) -> ToolHandler:
            tool_name = name or fn.__name__
            schema = input_schema or _infer_schema(fn)
            tool_def = ToolDef(
                name=tool_name,
                description=description or fn.__doc__ or "",
                inputSchema=schema,
            )
            self._tools[tool_name] = (tool_def, fn)
            return fn
        return decorator

    def resource(
        self,
        uri: str,
        *,
        name: str | None = None,
        description: str = "",
        mime_type: str = "text/plain",
    ) -> Callable[[ResourceHandler], ResourceHandler]:
        """注册 MCP resource（装饰器）。"""
        def decorator(fn: ResourceHandler) -> ResourceHandler:
            res_name = name or fn.__name__
            res_def = ResourceDef(
                uri=uri,
                name=res_name,
                description=description or fn.__doc__ or "",
                mimeType=mime_type,
            )
            self._resources[uri] = (res_def, fn)
            return fn
        return decorator

    def prompt(
        self,
        name: str | None = None,
        *,
        description: str = "",
        arguments: list[dict[str, Any]] | None = None,
    ) -> Callable[[PromptHandler], PromptHandler]:
        """注册 MCP prompt（装饰器）。"""
        def decorator(fn: PromptHandler) -> PromptHandler:
            prompt_name = name or fn.__name__
            prompt_def = PromptDef(
                name=prompt_name,
                description=description or fn.__doc__ or "",
                arguments=arguments or [],
            )
            self._prompts[prompt_name] = (prompt_def, fn)
            return fn
        return decorator

    # --- MCP handlers ---

    async def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        capabilities = ServerCapabilities(
            tools={"listChanged": False} if self._tools else None,
            resources={"subscribe": False, "listChanged": False} if self._resources else None,
            prompts={"listChanged": False} if self._prompts else None,
        )
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": capabilities.to_dict(),
            "serverInfo": {
                "name": self.name,
                "version": self.version,
            },
            **({"instructions": self.instructions} if self.instructions else {}),
        }

    async def _handle_initialized(self, params: dict[str, Any]) -> None:
        pass  # 客户端确认初始化完成

    async def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": [td.to_dict() for td, _ in self._tools.values()]}

    async def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = params.get("name")
        if not tool_name or tool_name not in self._tools:
            raise JsonRpcError(INVALID_PARAMS, f"Unknown tool: {tool_name}")

        _, handler = self._tools[tool_name]
        arguments = params.get("arguments", {})

        try:
            result = await handler(**arguments)
        except TypeError as e:
            raise JsonRpcError(INVALID_PARAMS, f"Invalid arguments: {e}") from e
        except Exception as e:
            logger.exception("Tool %s raised exception", tool_name)
            result = ToolResult.error(str(e))

        if isinstance(result, str):
            result = ToolResult.text(result)
        return result.to_dict()

    async def _handle_resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"resources": [rd.to_dict() for rd, _ in self._resources.values()]}

    async def _handle_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not uri or uri not in self._resources:
            raise JsonRpcError(INVALID_PARAMS, f"Unknown resource: {uri}")

        _, handler = self._resources[uri]
        content = await handler(uri)
        if isinstance(content, dict):
            return {"contents": [content]}
        return {"contents": content if isinstance(content, list) else [{"uri": uri, "text": str(content)}]}

    async def _handle_prompts_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"prompts": [pd.to_dict() for pd, _ in self._prompts.values()]}

    async def _handle_prompts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        prompt_name = params.get("name")
        if not prompt_name or prompt_name not in self._prompts:
            raise JsonRpcError(INVALID_PARAMS, f"Unknown prompt: {prompt_name}")

        _, handler = self._prompts[prompt_name]
        arguments = params.get("arguments", {})
        messages = await handler(**arguments)
        return {"messages": messages}

    # --- ASGI endpoint ---

    async def handle_request(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        """处理 MCP HTTP 端点请求。"""
        method = scope.get("method", "GET")

        if method == "POST":
            await self._handle_post(scope, receive, send)
        elif method == "GET":
            await self._handle_get(scope, receive, send)
        elif method == "DELETE":
            await self._handle_delete(scope, receive, send)
        else:
            await _send_json_response(send, 405, {"error": "Method not allowed"})

    async def _handle_post(self, scope: dict, receive: Any, send: Any) -> None:
        """POST — 接收 JSON-RPC 消息，返回 JSON 或 SSE。"""
        # 读取 body
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break

        # 解析 JSON-RPC
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            await _send_json_response(send, 400, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            })
            return

        # 判断是否含有 request（有 id + method）
        messages = parsed if isinstance(parsed, list) else [parsed]
        has_request = any(
            isinstance(m, dict) and "id" in m and "method" in m
            for m in messages
        )

        if not has_request:
            # 纯 notification/response → 处理后返回 202
            for msg in messages:
                if isinstance(msg, dict):
                    await self._dispatch.handle(msg)
            await _send_empty_response(send, 202)
            return

        # 含有 request → 检查 Accept 头决定返回 JSON 还是 SSE
        headers = dict(scope.get("headers", []))
        accept = headers.get(b"accept", b"").decode()

        # 处理消息
        if isinstance(parsed, list):
            responses = []
            for msg in parsed:
                if isinstance(msg, dict):
                    resp = await self._dispatch.handle(msg)
                    if resp is not None:
                        responses.append(resp)
            result_data = responses if len(responses) != 1 else responses[0]
        else:
            result_data = await self._dispatch.handle(parsed)

        if result_data is None:
            await _send_empty_response(send, 202)
            return

        # 检查是否是 initialize 响应 → 生成 session ID
        extra_headers: list[tuple[bytes, bytes]] = []
        if isinstance(parsed, dict) and parsed.get("method") == "initialize":
            session_id = secrets.token_hex(16)
            session = _MCPSession(session_id=session_id)
            self._sessions[session_id] = session
            extra_headers.append((b"mcp-session-id", session_id.encode()))

        if "text/event-stream" in accept:
            # SSE 响应
            await _send_sse_response(send, result_data, extra_headers)
        else:
            # JSON 响应
            await _send_json_response(send, 200, result_data, extra_headers)

    async def _handle_get(self, scope: dict, receive: Any, send: Any) -> None:
        """GET — 打开 SSE 流供 server → client 通信（当前返回 405）。"""
        # 基础实现暂不支持 server → client 主动推送
        await _send_json_response(send, 405, {"error": "GET not supported"})

    async def _handle_delete(self, scope: dict, receive: Any, send: Any) -> None:
        """DELETE — 终止 session。"""
        headers = dict(scope.get("headers", []))
        session_id = headers.get(b"mcp-session-id", b"").decode()
        if session_id and session_id in self._sessions:
            del self._sessions[session_id]
            await _send_empty_response(send, 200)
        else:
            await _send_empty_response(send, 404)


class _MCPSession:
    """MCP session 状态。"""
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.initialized = False


def mount_mcp(app: Any, path: str, mcp: MCPServer) -> Any:
    """将 MCPServer 挂载到 ASGI app 的指定路径。

    返回一个新的 ASGI app，会拦截 path 前缀的请求交给 MCP 处理。
    """
    path = path.rstrip("/")

    async def mcp_app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == path:
            await mcp.handle_request(scope, receive, send)
        else:
            await app(scope, receive, send)

    return mcp_app


# --- 内部辅助 ---

async def _send_json_response(
    send: Any,
    status: int,
    data: Any,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(data).encode()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _send_empty_response(send: Any, status: int) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-length", b"0")],
    })
    await send({"type": "http.response.body", "body": b""})


async def _send_sse_response(
    send: Any,
    data: Any,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """发送 SSE 格式的 JSON-RPC 响应。"""
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"text/event-stream"),
        (b"cache-control", b"no-cache"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": 200, "headers": headers})

    sse_data = format_sse(json.dumps(data), event="message")
    await send({"type": "http.response.body", "body": sse_data, "more_body": False})


def _infer_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    """从函数签名推断 JSON Schema（简易版）。"""
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
    }

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        annotation = param.annotation
        json_type = type_map.get(annotation, "string")
        properties[name] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
