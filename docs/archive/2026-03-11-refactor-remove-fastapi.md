# 去掉 FastAPI — 轻量路由 + Declaration 自动发现 设计规范

**状态**：✅ 已完成
**日期**：2026-03-11
**类型**：重构

## 背景

自研 ASGI Server（`mutbot.server`）已完全替代 uvicorn，FastAPI 在当前架构中被严重低估使用：

- **RPC 分发绕过 FastAPI**：WebSocket 消息到达后直接走 `RpcDispatcher.dispatch()`，FastAPI 只做了 WebSocket accept
- **零 Pydantic 使用**：甚至显式写了 `response_model=None` 来禁用
- **HTTP 端点极少**：只有 `/api/health` 和 `/llm/*` 几个
- **闭包问题**：RPC handler 拆分后变成 `register_xxx_rpc()` 的闭包函数，无法直接 import 测试

去掉 FastAPI 后可移除的依赖链：

```
fastapi
├── starlette        （完整 ASGI 框架，最大的间接依赖）
├── pydantic         （项目零使用，anthropic SDK 也未使用）
│   ├── pydantic-core（Rust 编译的二进制包，最重）
│   └── annotated-types
├── typing-inspection
└── annotated-doc
```

净移除 **6 个包**（fastapi、starlette、pydantic、pydantic-core、annotated-types、annotated-doc），其中 pydantic-core 是 Rust 编译的二进制包，体积最大。

## 设计方案

### 统一设计原则：ASGI 风格贯穿全栈

每一层都是 `(scope, receive, send)` 的翻译器，接收一个 callable，处理协议细节，构造更高级的 scope，传给下一层：

```
Server          — TCP bytes  → ASGI scope         （纯传输，无状态）
Router          — ASGI scope → View / WebSocketView 分发（应用层，ASGI app）
handle_mcp()    — ASGI scope → MCP scope           （协议翻译，无状态纯函数）
handler         — MCP scope  → 业务逻辑            （状态在这里）
```

### ASGI 接口保留决策

保留 ASGI 作为 `mutbot.server` 的输出接口（`_http.py` 和 `_ws.py` 已实现的桥接代码不变）。Router 本身实现 ASGI 协议（`__call__(scope, receive, send)`），作为 ASGI app 传给 Server。

**理由**：
- `mutbot.server` 是通用 server 包，保持 ASGI 兼容利于复用和第三方中间件集成
- `_http.py` 的 `RequestResponseCycle` 和 `_ws.py` 的 `WSProtocol` 已稳定工作，改动成本 > 收益
- 业务代码不直接面对 ASGI——Router 内部翻译 scope 为友好的 `Request` 对象

### 架构分层

```
mutbot.server（纯传输层，等价于 uvicorn 的定位）
├── Server          — TCP 监听、lifespan 协议
├── HTTPProtocol    — h11 解析 → ASGI scope/receive/send
└── WSProtocol      — wsproto 帧 → ASGI WebSocket
    不含路由、不含信号处理、零业务依赖。
    输入：ASGI app callable。输出：标准 ASGI 协议。
    MCP 协议翻译（handle_mcp）也在此层，因为它是通用协议处理，不含业务逻辑。

mutbot.web（应用层，等价于 starlette 的定位）
├── view.py                         — 框架：View / WebSocketView 基类、Request、Response、Router（新建）
│   ⚠ 可独立搭配 mutbot.server 使用，仅依赖 mutobj，不依赖 mutbot.web 其余业务模块
├── rpc.py                          — 框架：RpcDispatcher、RpcContext、AppRpc/WorkspaceRpc/SessionRpc 基类
├── routes.py                       — 业务：HealthView、AppWebSocket、WorkspaceWebSocket（保留原文件名）
├── rpc_app.py                      — 业务：WorkspaceOps(AppRpc)、FilesystemOps(AppRpc)
├── rpc_session.py                  — 业务：SessionOps(SessionRpc)
├── rpc_workspace.py                — 业务：TerminalOps(WorkspaceRpc)、MenuOps(WorkspaceRpc)
└── server.py                       — 组装 Router、安装信号处理、传给 Server 启动

mutbot.proxy（LLM 代理，也通过 Declaration 注册路由）
└── routes.py                       — 业务：LlmInfoView、LlmModelsView 等（保留原文件名）
```

### mutbot.server 职责边界

`mutbot.server` 只做传输层的事：

- **TCP 监听**（`Server.startup`）
- **HTTP/1.1 解析**（`HTTPProtocol` + h11）
- **WebSocket 帧处理**（`WSProtocol` + wsproto）
- **ASGI 桥接**（scope/receive/send 构造）
- **Lifespan 协议**（startup/shutdown 事件传递）
- **Graceful shutdown**（停止接受连接、cancel ASGI task、等待活跃连接完成）
- **MCP 协议翻译**（`handle_mcp` 纯函数，JSON-RPC over HTTP ↔ MCP scope）

**不做**：
- 路由分发 → 移到 `mutbot.web.Router`
- 信号处理（SIGINT/SIGTERM） → 移到 `mutbot.web.server`（应用层启动入口）
- 中间件、依赖注入、请求验证
- MCP 业务逻辑（tool 注册/发现/执行）

信号处理移出后，`Server` 只暴露 `should_exit` 属性供外部设置，`run()` 方法接受可选的 `on_signal` 回调或由调用方自行安装信号处理器。

### 核心设计

#### MCP 协议翻译（mutbot.server 层）

`handle_mcp` 是纯函数，直接处理 ASGI scope 并调用 MCP handler。只做 JSON-RPC over HTTP 的协议翻译，不持有状态，不知道有哪些 tool：

```python
async def handle_mcp(scope, receive, send, handler):
    """ASGI → MCP 协议翻译。纯函数，无状态。"""
    # HTTP POST → 读 body → 解析 JSON-RPC → 构造 MCP scope
    mcp_scope = {
        "type": "mcp",
        "method": parsed["method"],       # MCP 原始 method，如 "tools/call"
        "params": parsed.get("params", {}),
    }

    # 调 handler，收集 send 结果
    # 根据 Accept 头决定返回 JSON 或 SSE
    ...
```

MCP handler 签名遵循 ASGI 风格，scope 是处理过的（JSON-RPC 已解析）：

```python
async def my_mcp(scope, receive, send):
    method = scope["method"]    # MCP 原始 method name
    params = scope["params"]    # MCP 原始 params

    if method == "initialize":
        await send({"serverInfo": {"name": "demo", "version": "0.1.0"},
                    "capabilities": {"tools": {}}})

    elif method == "tools/list":
        await send({"tools": [...]})

    elif method == "tools/call":
        name = params["name"]
        args = params.get("arguments", {})
        # 执行 tool ...
        await send({"content": [{"type": "text", "text": "result"}]})
```

**Streamable HTTP 支持**：handler 多次调用 `send()` 即为流式。`handle_mcp` 根据客户端 `Accept` 头决定传输方式：

- `Accept: application/json` → 取最终 result，返回 JSON
- `Accept: text/event-stream` → 每次 `send()` 立即推一条 SSE 事件

```python
async def my_mcp(scope, receive, send):
    if scope["method"] == "tools/call":
        # 中间通知（进度等）
        await send({"type": "notification",
                    "method": "notifications/progress",
                    "params": {"progress": 50}})
        # 最终结果
        await send({"type": "result",
                    "content": [{"type": "text", "text": "done"}]})
```

handler 不知道传输方式（JSON vs SSE），只管调 `send()`。

#### MCP 与 Declaration 动态发现的集成

MCP handler（`my_mcp`）在 `mutbot.web` 层实现，通过 Declaration 动态发现 tool：

```python
# mutbot.web 层
class MCPToolSet(mutobj.Declaration):
    """MCP tool 集合基类。一个类定义一组 tool，方法名就是 tool name。"""
    prefix: str = ""  # 可选前缀，如 "math_"，拼接为 "math_add"

class MCPToolProvider:
    """generation 检查 + 懒刷新，桥接 Declaration 发现到 MCP handler。"""
    _gen: int = -1
    _tools: dict[str, tuple[MCPToolSet, str]] = {}  # tool_name → (instance, method_name)

    def refresh(self):
        gen = mutobj.get_registry_generation()
        if gen != self._gen:
            self._gen = gen
            self._tools = {}
            for cls in mutobj.discover_subclasses(MCPToolSet):
                instance = cls()
                prefix = instance.prefix
                for name in _get_tool_methods(cls):
                    tool_name = f"{prefix}{name}" if prefix else name
                    self._tools[tool_name] = (instance, name)

    def list_tools(self) -> list[dict]:
        """从类型注解 + docstring 自动生成 tool schema。"""
        ...

    async def call_tool(self, name: str, args: dict) -> ...:
        instance, method_name = self._tools[name]
        return await getattr(instance, method_name)(**args)
```

**schema 自动提取**：从方法的类型注解和 docstring 生成 MCP tool schema：
- 方法名（+ prefix）→ `name`
- docstring → `description`
- 类型注解 → `inputSchema`（`int` → `integer`，`str` → `string`，`bool` → `boolean`，`float` → `number`）
- 无默认值的参数 → `required`

**用法示例**：

```python
class MathTools(MCPToolSet):
    prefix = "math_"

    async def add(self, a: int, b: int) -> str:
        """Add two numbers."""
        return str(a + b)

    async def multiply(self, a: int, b: int) -> str:
        """Multiply two numbers."""
        return str(a * b)

class SearchTools(MCPToolSet):
    # 无 prefix，tool name 就是方法名

    async def search(self, query: str, limit: int = 10) -> str:
        """Search for something."""
        ...
```

生成的 tool schema：`math_add`、`math_multiply`、`search`。

MCP handler 内部使用 provider：

```python
provider = MCPToolProvider()

async def my_mcp(scope, receive, send):
    method = scope["method"]
    params = scope["params"]

    if method == "tools/list":
        provider.refresh()
        await send({"tools": provider.list_tools()})

    elif method == "tools/call":
        provider.refresh()
        result = await provider.call_tool(params["name"], params.get("arguments", {}))
        await send(result)
```

MCPView 中使用 `handle_mcp`：

```python
class MCPView(View):
    path = "/mcp"

    async def post(self, request: Request) -> Response:
        await handle_mcp(request._scope, request._receive, request._send, my_mcp)
```

#### Router（mutbot.web 层）

放在 `mutbot.web.view` 中，实现 ASGI 接口，作为 ASGI app 传给 Server：

- 精确路径匹配 + `{param}` 路径参数（解析结果放 `request.path_params`）
- HTTP 请求按 method 调用 View 实例的对应方法（`get`/`post`/...）
- WebSocket upgrade 调用 WebSocketView 实例的 `connect` 方法
- 静态文件 fallback

**不做**：中间件链、依赖注入、请求验证、OpenAPI 生成。

启动时自动发现并注册：

```python
router = Router()
for view_cls in mutobj.discover_subclasses(View):
    view = view_cls()
    router.add(view.path, view)
for ws_cls in mutobj.discover_subclasses(WebSocketView):
    ws_view = ws_cls()
    router.add_ws(ws_view.path, ws_view)
router.add_static("/", frontend_dist_dir)
```

Router 内部分发：收到 HTTP 请求 → 匹配 path → 按 HTTP method 调对应方法（`view.get(request)`、`view.post(request)`）。收到 WebSocket upgrade → 匹配 path → 调 `ws_view.connect(ws)`。未实现的 HTTP 方法返回基类默认的 405。

#### Request / Response（mutbot.web 层）

轻量封装，替代 FastAPI 的 Request/WebSocket/Response 类：

```python
class Request:
    method: str
    path: str
    headers: dict[str, str]
    query_params: dict[str, str]
    path_params: dict[str, str]     # Router 解析的 {param} 路径参数

    async def body(self) -> bytes: ...
    async def json(self) -> Any: ...

class Response:
    status: int
    headers: dict[str, str]
    body: bytes

def json_response(data, status=200) -> Response: ...
def html_response(html, status=200) -> Response: ...

class StreamingResponse:
    status: int
    headers: dict[str, str]
    body_iterator: AsyncIterator[bytes]

class WebSocketConnection:
    path: str
    query_params: dict[str, str]
    path_params: dict[str, str]

    async def accept(self) -> None: ...
    async def receive(self) -> dict: ...
    async def receive_json(self) -> Any: ...
    async def send_json(self, data) -> None: ...
    async def send_bytes(self, data) -> None: ...
    async def close(self, code=1000, reason="") -> None: ...
```

#### View Declaration（mutbot.web 层）

HTTP 路由，一个 path 一个类，方法名就是 HTTP method：

```python
class View(mutobj.Declaration):
    """HTTP 路由声明基类。"""
    path: str = ...

    async def get(self, request: Request) -> Response:
        return Response(status=405)

    async def post(self, request: Request) -> Response:
        return Response(status=405)

    async def put(self, request: Request) -> Response:
        return Response(status=405)

    async def delete(self, request: Request) -> Response:
        return Response(status=405)
```

子类只覆盖需要的方法，未覆盖的自动返回 405。

#### WebSocketView Declaration（mutbot.web 层）

WebSocket 路由，单独基类，一个 `connect` 方法接管整个连接生命周期：

```python
class WebSocketView(mutobj.Declaration):
    """WebSocket 路由声明基类。"""
    path: str = ...

    async def connect(self, ws: WebSocketConnection) -> None:
        """WebSocket 生命周期入口。接管整个连接，方法返回即断开。"""
        await ws.close(code=4405, reason="Not implemented")
```

**用法示例**：

```python
# 简单 GET
class HealthView(View):
    path = "/api/health"

    async def get(self, request: Request) -> Response:
        return json_response({"status": "ok"})

# 简单 POST
class LlmMessagesView(View):
    path = "/llm/v1/messages"

    async def post(self, request: Request) -> Response:
        body = await request.json()
        return await proxy_request(body, client_format="anthropic")

# 带路径参数
class SessionView(View):
    path = "/api/session/{session_id}"

    async def get(self, request: Request) -> Response:
        sid = request.path_params["session_id"]
        ...

# 同一 path 支持多个 method
class MCPView(View):
    path = "/mcp"

    async def post(self, request: Request) -> Response:
        ...  # JSON-RPC 请求

    async def delete(self, request: Request) -> Response:
        ...  # 终止 session

# WebSocket（简单）
class AppWebSocket(WebSocketView):
    path = "/ws/app"

    async def connect(self, ws: WebSocketConnection) -> None:
        await ws.accept()
        dispatcher = RpcDispatcher.from_declaration(AppRpc)
        ctx = RpcContext(...)
        while True:
            raw = await ws.receive_json()
            response = await dispatcher.dispatch(raw, ctx)
            if response:
                await ws.send_json(response)

# WebSocket（复杂，含 Client 管理、重连、Channel 等）
class WorkspaceWebSocket(WebSocketView):
    path = "/ws/workspace/{workspace_id}"

    async def connect(self, ws: WebSocketConnection) -> None:
        workspace_id = ws.path_params["workspace_id"]
        await ws.accept()
        # Client 注册、重连、Binary Frame、Channel...
```

#### RPC Declaration（mutbot.web 层）

RPC 方法按端点级别用不同基类区分，一个命名空间一个类，方法名就是 RPC method 名：

```python
class AppRpc(mutobj.Declaration):
    """App 级 RPC 基类（/ws/app 端点）。"""
    namespace: str = ...

class WorkspaceRpc(mutobj.Declaration):
    """Workspace 级 RPC 基类（/ws/workspace/{id} 端点，非 session 相关）。"""
    namespace: str = ...

class SessionRpc(mutobj.Declaration):
    """Session 级 RPC 基类（workspace 内，session 相关操作）。"""
    namespace: str = ...
```

**用法示例**：

```python
# --- App 级 ---

class WorkspaceOps(AppRpc):
    namespace = "workspace"

    async def list(self, params: dict, ctx: RpcContext) -> list[dict]:
        wm = ctx.workspace_manager
        if not wm:
            return []
        return [workspace_dict(ws) for ws in wm.list_all()]

    async def create(self, params: dict, ctx: RpcContext) -> dict:
        ...

    async def remove(self, params: dict, ctx: RpcContext) -> dict:
        ...


class FilesystemOps(AppRpc):
    namespace = "filesystem"

    async def browse(self, params: dict, ctx: RpcContext) -> dict:
        ...


# --- Workspace 级 ---

class TerminalOps(WorkspaceRpc):
    namespace = "terminal"

    async def create(self, params: dict, ctx: RpcContext) -> dict:
        ...

    async def resize(self, params: dict, ctx: RpcContext) -> dict:
        ...

    async def input(self, params: dict, ctx: RpcContext) -> dict:
        ...


class MenuOps(WorkspaceRpc):
    namespace = "menu"

    async def query(self, params: dict, ctx: RpcContext) -> list[dict]:
        ...

    async def execute(self, params: dict, ctx: RpcContext) -> dict:
        ...


# --- Session 级 ---

class SessionOps(SessionRpc):
    namespace = "session"

    async def connect(self, params: dict, ctx: RpcContext) -> dict:
        ...

    async def create(self, params: dict, ctx: RpcContext) -> dict:
        ...

    async def delete(self, params: dict, ctx: RpcContext) -> dict:
        ...
```

方法名映射规则：`{namespace}.{method_name}`，如 `WorkspaceOps.list` → `"workspace.list"`，`SessionOps.connect` → `"session.connect"`。

**RpcDispatcher 自动发现**：

```python
class RpcDispatcher:
    @classmethod
    def from_declaration(cls, *base_classes) -> RpcDispatcher:
        """自动发现指定基类的所有子类，注册其公开方法。"""
        dispatcher = cls()
        for base in base_classes:
            for rpc_cls in mutobj.discover_subclasses(base):
                instance = rpc_cls()
                ns = instance.namespace
                for name in _get_rpc_methods(rpc_cls):
                    dispatcher.register(f"{ns}.{name}", getattr(instance, name))
        return dispatcher
```

WebSocket View 使用 dispatcher：

```python
class AppWebSocket(WebSocketView):
    path = "/ws/app"

    async def connect(self, ws: WebSocketConnection) -> None:
        await ws.accept()
        dispatcher = RpcDispatcher.from_declaration(AppRpc)
        ctx = RpcContext(workspace_id="", ...)
        while True:
            raw = await ws.receive_json()
            response = await dispatcher.dispatch(raw, ctx)
            if response:
                await ws.send_json(response)


class WorkspaceWebSocket(WebSocketView):
    path = "/ws/workspace/{workspace_id}"

    async def connect(self, ws: WebSocketConnection) -> None:
        workspace_id = ws.path_params["workspace_id"]
        await ws.accept()
        dispatcher = RpcDispatcher.from_declaration(WorkspaceRpc, SessionRpc)
        ctx = RpcContext(workspace_id=workspace_id, ...)
        while True:
            raw = await ws.receive_json()
            response = await dispatcher.dispatch(raw, ctx)
            if response:
                await ws.send_json(response)
```

**闭包问题彻底消除**：
- 每个 RPC handler 是独立的 Declaration 子类，可直接 import 测试
- 不需要 `register_xxx_rpc()` 函数
- 新增 RPC method 只需定义子类，零注册

#### mutobj 动态特性支持

View、WebSocketView 和 RPC Declaration 均为 `mutobj.Declaration` 子类，天然支持 mutobj 的全部动态能力：

**动态发现（`discover_subclasses`）**：Router 和 RpcDispatcher 通过 `mutobj.discover_subclasses()` 自动发现所有已注册的子类。新增路由或 RPC 方法只需定义子类，import 即生效，零手动注册。

**动态定义**：运行时可通过 `type()` 或其他方式动态创建 View / WebSocketView / RPC 子类，创建后立即进入 `_class_registry`，下次 `discover_subclasses()` 即可发现。配合 `get_registry_generation()` 的 generation 检查，Router 和 RpcDispatcher 可感知新注册并懒刷新。

**`@impl` 覆盖**：View、WebSocketView 和 RPC 的方法可被 `@impl` 覆盖。例如：

```python
# 原始定义
class HealthView(View):
    path = "/api/health"
    async def get(self, request: Request) -> Response:
        return json_response({"status": "ok"})

# 扩展项目覆盖实现（不修改原始代码）
@impl(HealthView)
def get(self, request: Request) -> Response:
    return json_response({"status": "ok", "extra": "info"})
```

同理，RPC 方法也可被 `@impl` 覆盖，上层项目可在不修改核心代码的情况下扩展或替换任意 handler。

**generation 感知刷新**：Router 和 RpcDispatcher 缓存发现结果，通过 `mutobj.get_registry_generation()` 检测注册表变化，仅在有新子类注册时才重新发现。与 MCPToolProvider 采用相同模式：

```python
class Router:
    _gen: int = -1
    _views: dict[str, View] = {}
    _ws_views: dict[str, WebSocketView] = {}

    def _refresh(self):
        gen = mutobj.get_registry_generation()
        if gen != self._gen:
            self._gen = gen
            self._views = {}
            self._ws_views = {}
            for view_cls in mutobj.discover_subclasses(View):
                view = view_cls()
                self._views[view.path] = view
            for ws_cls in mutobj.discover_subclasses(WebSocketView):
                ws_view = ws_cls()
                self._ws_views[ws_view.path] = ws_view
```

### 当前 FastAPI 使用点迁移对照

| 当前实现 | 迁移方案 |
|---------|---------|
| `@router.get("/api/health")` | `HealthView(View)` — `get()` |
| `@router.websocket("/ws/app")` | `AppWebSocket(WebSocketView)` — `connect()` |
| `@router.websocket("/ws/workspace/{workspace_id}")` | `WorkspaceWebSocket(WebSocketView)` — `connect()` |
| `@router.get("/llm")` + `@router.get("/llm/")` | `LlmInfoView(View)` — `get()` |
| `@router.get("/llm/v1/models")` | `LlmModelsView(View)` — `get()` |
| `@router.post("/llm/v1/messages")` | `LlmMessagesView(View)` — `post()` |
| `@router.post("/llm/v1/chat/completions")` | `LlmCompletionsView(View)` — `post()` |
| `MCPServer` + `mount_mcp()` | `MCPView(View)` — `post()` + `handle_mcp` + MCPToolSet |
| `StaticFiles("/")` | `Router.add_static()` |
| `FastAPI(lifespan=...)` | `Server` 已有 lifespan 支持 |
| `app.state.config` | 直接从 `mutbot.web.server` 模块级变量读取（现有 `_get_managers()` 模式） |

### 裸 ASGI 用法示例

不使用 Router，直接基于 `mutbot.server` 构建应用：

```python
from mutbot.server import Server, handle_mcp

async def my_mcp(scope, receive, send):
    method = scope["method"]
    params = scope["params"]

    if method == "initialize":
        await send({"serverInfo": {"name": "demo", "version": "0.1.0"},
                    "capabilities": {"tools": {}}})
    elif method == "tools/list":
        await send({"tools": [
            {"name": "add", "description": "Add two numbers",
             "inputSchema": {"type": "object",
                             "properties": {"a": {"type": "integer"},
                                            "b": {"type": "integer"}},
                             "required": ["a", "b"]}},
        ]})
    elif method == "tools/call":
        a = params["arguments"]["a"]
        b = params["arguments"]["b"]
        await send({"content": [{"type": "text", "text": str(a + b)}]})

async def app(scope, receive, send):
    if scope["type"] == "http":
        path, method = scope["path"], scope["method"]
        if path == "/status" and method == "GET":
            body = b'{"ok":true}'
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
        elif path == "/mcp" and method == "POST":
            await handle_mcp(scope, receive, send, my_mcp)
        else:
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})

Server(app).run(host="127.0.0.1", port=8000)
```

## 待定问题

（无）

## 实施步骤清单

### Phase 1: 框架层 [✅ 已完成]

- [x] **Task 1.1**: 新建 `mutbot/web/view.py` — 框架基础
  - [x] View 基类（Declaration 子类，path + get/post/put/delete 默认返回 405）
  - [x] WebSocketView 基类（Declaration 子类，path + connect 默认关闭）
  - [x] Request 类（method, path, headers, query_params, path_params, body(), json()）
  - [x] Response 类 + json_response / html_response 辅助函数
  - [x] StreamingResponse 类
  - [x] WebSocketConnection 类（accept, receive, receive_json, send_json, send_bytes, close）
  - [x] Router 类（ASGI app，discover View/WebSocketView，path 匹配 + {param} 支持，静态文件 fallback）
  - 状态：✅ 已完成

- [x] **Task 1.2**: 更新 `mutbot/web/rpc.py` — RPC 框架扩展
  - [x] 新增 AppRpc / WorkspaceRpc / SessionRpc 基类（Declaration 子类，namespace 属性）
  - [x] RpcDispatcher 新增 `from_declaration(*base_classes)` 类方法
  - [x] `_get_rpc_methods()` 辅助函数（过滤公开方法，排除基类方法）
  - 状态：✅ 已完成

### Phase 2: 业务层迁移 [✅ 已完成]

- [x] **Task 2.4**: 迁移 `mutbot/web/routes.py` — FastAPI → View/WebSocketView（最高风险，优先验证）
  - [x] 去掉 FastAPI 依赖（APIRouter, WebSocket, WebSocketDisconnect）
  - [x] `/api/health` → HealthView(View)
  - [x] `/ws/app` → AppWebSocket(WebSocketView)，使用 RpcDispatcher.from_declaration(AppRpc)
  - [x] `/ws/workspace/{id}` → WorkspaceWebSocket(WebSocketView)，使用 from_declaration(WorkspaceRpc, SessionRpc)
  - [x] Client 注册表、广播等辅助逻辑保留
  - 状态：✅ 已完成

- [x] **Task 2.1**: 迁移 `mutbot/web/rpc_app.py` — 闭包 → AppRpc 子类
  - [x] `register_app_rpc()` 内的闭包函数改为 AppRpc 子类的方法
  - [x] 删除 `register_app_rpc()` 函数
  - 状态：✅ 已完成

- [x] **Task 2.2**: 迁移 `mutbot/web/rpc_workspace.py` — 闭包 → WorkspaceRpc 子类
  - [x] `register_workspace_rpc()` 内的闭包函数改为 WorkspaceRpc 子类的方法
  - [x] 删除 `register_workspace_rpc()` 函数
  - 状态：✅ 已完成

- [x] **Task 2.3**: 迁移 `mutbot/web/rpc_session.py` — 闭包 → SessionRpc 子类
  - [x] `register_session_rpc()` 内的闭包函数改为 SessionRpc 子类的方法
  - [x] 删除 `register_session_rpc()` 函数
  - 状态：✅ 已完成

- [x] **Task 2.5**: 迁移 `mutbot/proxy/routes.py` — FastAPI → View 子类
  - [x] `create_llm_router()` 闭包模式改为 View 子类（LlmInfoView、LlmModelsView、LlmMessagesView、LlmCompletionsView）
  - [x] 去掉 FastAPI 依赖（APIRouter, Request, JSONResponse, HTMLResponse, StreamingResponse）
  - 状态：✅ 已完成

### Phase 3: 启动入口 + Server 层清理 [✅ 已完成]

- [x] **Task 3.1**: 重构 `mutbot/web/server.py` — 去掉 FastAPI，使用 Router
  - [x] 移除 `FastAPI` / `StaticFiles` 导入和使用
  - [x] lifespan 改为独立 async context manager（不再接收 FastAPI app 参数）
  - [x] 用 Router 替代 FastAPI app，传给 Server
  - [x] `main()` 中组装 Router、注册静态文件
  - 状态：✅ 已完成

- [x] **Task 3.2**: 信号处理保留在 Server 层
  - [x] 信号处理已在 Server 中正常工作，无需移动
  - 状态：✅ 已完成

- [x] **Task 3.3**: 重构 `mutbot/server/_mcp_server.py` → `handle_mcp` + MCPToolSet
  - [x] 新增 `handle_mcp(scope, receive, send, handler)` 纯函数
  - [x] 新增 MCPToolSet（Declaration 子类）
  - [x] 新增 MCPToolProvider（generation 感知）
  - [x] 保留原 MCPServer 类不变
  - [x] 更新 `mutbot/server/__init__.py` 导出
  - 状态：✅ 已完成

### Phase 4: 清理 + 验证 [✅ 已完成]

- [x] **Task 4.1**: 移除 FastAPI 依赖
  - [x] `pyproject.toml` 移除 fastapi 依赖
  - [x] 全局搜索确认零 fastapi / starlette import
  - 状态：✅ 已完成

- [x] **Task 4.2**: 更新测试
  - [x] 修复 `test_workspace_selector.py` — 移除 `_RPC_SKIP`，改用 `WorkspaceOps` / `FilesystemOps` 直接调用
  - [x] 更新 `test_rpc.py` — `TestRpcDeclarationDiscovery` 使用 `from_declaration`
  - [x] 更新 `test_rpc_handlers.py` — `_dispatch()` 使用 `from_declaration`
  - [x] 更新 `test_runtime_menu.py` — 使用 `from_declaration` dispatcher
  - [x] 更新 `test_runtime_imports.py` — 导入 `main` 和 `HealthView` 替代旧接口
  - [x] 更新 `test_setup_integration.py` — `ConnectionManager` → 模块级函数测试
  - [x] 更新 `web/transport.py` — TYPE_CHECKING 中用 WebSocketConnection 替代 fastapi.WebSocket
  - [x] 删除 `web/connection.py`（遗留未使用）
  - 状态：✅ 已完成

- [x] **Task 4.3**: 构建 + 测试验证
  - [x] `pip install -e ".[dev]"` 确认无依赖问题
  - [x] `pytest` 467 项全部通过
  - [x] 全局零 fastapi/starlette import
  - 状态：✅ 已完成

## 测试验证

- `pip install -e ".[dev]"` — 成功安装，无 fastapi/starlette/pydantic 依赖
- `pytest` — 467 项全部通过（5.75s）
- 全局搜索 `src/` — 零 fastapi / starlette import
- 修复发现的问题：
  - `from_declaration` 中 namespace 属性需从类字典读取（`rpc_cls.__dict__`），因为 mutobj Declaration 初始化会重置实例属性为基类默认值
  - `test_setup_integration.py` 中 `ConnectionManager` 测试改为测试模块级 `queue_workspace_event` / `_pop_pending_events` 函数

## 关键参考

### 源码

- `src/mutbot/server/_http.py` — HTTPProtocol + RequestResponseCycle（ASGI 桥接）
- `src/mutbot/server/_ws.py` — WSProtocol（WebSocket ASGI 桥接）
- `src/mutbot/server/_server.py` — Server 类（TCP + lifespan + 信号处理待移出）
- `src/mutbot/server/_mcp_server.py` — 当前 MCPServer（待重构为 handle_mcp 纯函数 + MCPToolSet）
- `src/mutbot/web/server.py` — FastAPI app 创建 + 启动入口
- `src/mutbot/web/routes.py` — 当前路由（health + 2 个 WebSocket 端点）
- `src/mutbot/web/rpc_app.py` — App RPC handler（闭包模式，待消除）
- `src/mutbot/web/rpc_session.py` — Session RPC handler（闭包模式，待消除）
- `src/mutbot/web/rpc_workspace.py` — Workspace RPC handler（闭包模式，待消除）
- `src/mutbot/proxy/routes.py` — LLM 代理路由（闭包模式，待消除）
- `src/mutbot/menu.py` — Menu Declaration（Route 设计的参考范例）
- `src/mutbot/runtime/menu_impl.py` — MenuRegistry（discover_subclasses 用法参考）

### 相关规范

- `docs/specifications/feature-asgi-server.md` — 自研 ASGI Server 设计规范
