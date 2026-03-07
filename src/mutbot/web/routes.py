"""API routes — REST endpoints and WebSocket handler."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mutbot.web.rpc import RpcDispatcher, RpcContext, make_event
from mutbot.web.transport import Client, decode_varint
from mutbot.runtime.menu_impl import menu_registry
from mutbot.menu import MenuResult

logger = logging.getLogger(__name__)

router = APIRouter()

# Workspace pending events: events queued before any client connects
# (e.g., setup flow creates session → event flushed on first WS connect)
_workspace_pending_events: dict[str, list[dict]] = {}


def queue_workspace_event(workspace_id: str, event: str, data: dict | None = None) -> None:
    """Queue an event for delivery when the first client connects to this workspace."""
    msg = {"type": "event", "event": event, "data": data or {}}
    _workspace_pending_events.setdefault(workspace_id, []).append(msg)


def _pop_pending_events(workspace_id: str) -> list[dict]:
    return _workspace_pending_events.pop(workspace_id, [])

# Client registry: client_id → Client (for reconnection matching)
_clients: dict[str, Client] = {}
# Workspace → connected Clients (for workspace-level broadcast via send buffer)
_workspace_clients: dict[str, set[Client]] = {}


def _broadcast_to_workspace(
    workspace_id: str, data: dict, *, exclude_client: Client | None = None,
) -> None:
    """广播到 workspace 的所有 Client（通过 send buffer，计入 total_sent）。"""
    clients = _workspace_clients.get(workspace_id)
    if not clients:
        return
    for c in list(clients):
        if c is exclude_client:
            continue
        c.send_json(data)


def _broadcast_to_all_workspaces(data: dict) -> None:
    """广播到所有 workspace 的所有 Client。"""
    for clients in _workspace_clients.values():
        for c in list(clients):
            c.send_json(data)


@router.get("/api/health")
async def health():
    """健康检查端点，返回服务状态和版本号。"""
    import mutbot
    return {"status": "ok", "version": mutbot.__version__}


# Workspace RPC dispatcher
workspace_rpc = RpcDispatcher()

# App-level RPC dispatcher (用于 /ws/app 全局端点)
app_rpc = RpcDispatcher()

# ---------------------------------------------------------------------------
# Origin 校验 — 接受 mutbot.ai 和 localhost
# ---------------------------------------------------------------------------

_ALLOWED_ORIGINS = {"https://mutbot.ai", "http://mutbot.ai"}


def _check_ws_origin(origin: str | None) -> bool:
    """校验 WebSocket Origin，接受 mutbot.ai 和 localhost。"""
    if not origin:
        return True  # 非浏览器客户端无 Origin
    if origin in _ALLOWED_ORIGINS:
        return True
    parsed = urlparse(origin)
    hostname = parsed.hostname or ""
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    return False


# ---------------------------------------------------------------------------
# App-level RPC handlers (workspace.list / workspace.create / filesystem.browse)
# ---------------------------------------------------------------------------

@app_rpc.method("workspace.list")
async def handle_app_workspace_list(params: dict, ctx: RpcContext) -> list[dict]:
    """列出所有工作区。"""
    wm = ctx.managers.get("workspace_manager")
    if not wm:
        return []
    return [_workspace_dict(ws) for ws in wm.list_all()]


@app_rpc.method("workspace.create")
async def handle_app_workspace_create(params: dict, ctx: RpcContext) -> dict:
    """创建工作区。

    params:
      - project_path (必填): 项目目录绝对路径
      - name (可选): 指定名称，否则从路径末段自动生成
    """
    wm = ctx.managers.get("workspace_manager")
    if not wm:
        return {"error": "workspace_manager not available"}

    project_path = params.get("project_path", "")
    if not project_path:
        return {"error": "missing project_path"}

    p = Path(project_path)
    if not p.is_absolute():
        return {"error": "project_path must be absolute"}
    if not p.is_dir():
        return {"error": "project_path does not exist or is not a directory"}

    name = params.get("name") or p.name
    ws = wm.create(name, str(p))

    # 无 LLM 配置时，创建默认 AgentSession 供配置向导使用
    _cfg = ctx.managers.get("config")
    if not _cfg or not _cfg.get("providers"):
        sm = ctx.managers.get("session_manager")
        if sm:
            agent_type = "mutbot.session.AgentSession"
            existing = sm.list_by_workspace(ws.id)
            agent_session = next(
                (s for s in existing
                 if s.type == agent_type),
                None,
            )
            if agent_session is None:
                agent_session = sm.create(ws.id, session_type=agent_type)
                ws.sessions.append(agent_session.id)
                wm.update(ws)
            # 前端连接后自动打开 tab
            queue_workspace_event(
                ws.id, "open_session", {"session_id": agent_session.id},
            )

    return _workspace_dict(ws)


@app_rpc.method("filesystem.browse")
async def handle_filesystem_browse(params: dict, ctx: RpcContext) -> dict:
    """列出目录内容（仅子目录）。

    params:
      - path (可选): 目录路径，空则返回用户主目录
    """
    path_str = params.get("path", "")
    if not path_str:
        target = Path.home()
    else:
        target = Path(path_str)

    if not target.is_dir():
        return {"error": f"not a directory: {path_str}"}

    resolved = target.resolve()
    parent = str(resolved.parent) if resolved.parent != resolved else None

    entries: list[dict[str, str]] = []
    try:
        for entry in sorted(resolved.iterdir(), key=lambda e: e.name.lower()):
            if entry.name.startswith('.'):
                continue
            if entry.is_dir():
                entries.append({"name": entry.name, "type": "dir"})
    except PermissionError:
        return {"error": f"permission denied: {resolved}"}

    return {
        "path": str(resolved),
        "parent": parent,
        "entries": entries,
    }


@app_rpc.method("workspace.remove")
async def handle_app_workspace_remove(params: dict, ctx: RpcContext) -> dict:
    """从注册表移除工作区（不删除数据文件）。

    params:
      - workspace_id (必填): 要移除的工作区 ID
    """
    wm = ctx.managers.get("workspace_manager")
    if not wm:
        return {"error": "workspace_manager not available"}

    workspace_id = params.get("workspace_id", "")
    if not workspace_id:
        return {"error": "missing workspace_id"}

    if not wm.remove(workspace_id):
        return {"error": "workspace not found"}
    return {"ok": True}


@app_rpc.method("menu.query")
async def handle_app_menu_query(params: dict, ctx: RpcContext) -> list[dict]:
    """App 级菜单查询。"""
    category = params.get("category", "")
    menu_context = params.get("context", {})
    ctx._menu_context = menu_context  # type: ignore[attr-defined]
    return menu_registry.query(category, ctx)


@app_rpc.method("menu.execute")
async def handle_app_menu_execute(params: dict, ctx: RpcContext) -> dict:
    """App 级菜单执行。"""
    menu_id = params.get("menu_id", "")
    if not menu_id:
        return {"error": "missing menu_id"}

    menu_cls = menu_registry.find_menu_class(menu_id)
    if menu_cls is None:
        return {"error": f"menu not found: {menu_id}"}

    menu_instance = menu_cls()
    execute_params = params.get("params", {})
    result = menu_instance.execute(execute_params, ctx)

    if not isinstance(result, MenuResult):
        return result if isinstance(result, dict) else {}

    result_dict: dict = {"action": result.action, "data": result.data}

    # workspace_removed: 从注册表移除
    if result.action == "workspace_removed":
        wm = ctx.managers.get("workspace_manager")
        ws_id = result.data.get("workspace_id", "")
        if wm and ws_id:
            wm.remove(ws_id)

    return result_dict


# ---------------------------------------------------------------------------
# Menu RPC handlers
# ---------------------------------------------------------------------------

@workspace_rpc.method("menu.query")
async def handle_menu_query(params: dict, ctx: RpcContext) -> list[dict]:
    """查询指定 category 的菜单项列表"""
    category = params.get("category", "")
    # 将前端传入的 context 注入到 RpcContext 以供 check_enabled/check_visible 使用
    menu_context = params.get("context", {})
    ctx._menu_context = menu_context  # type: ignore[attr-defined]
    return menu_registry.query(category, ctx)


@workspace_rpc.method("menu.execute")
async def handle_menu_execute(params: dict, ctx: RpcContext) -> dict:
    """执行指定菜单项。

    Menu.execute() 是同步方法，只做同步工作（如创建 session）。
    异步后续操作（stop、broadcast）由本 handler 统一处理。
    """
    menu_id = params.get("menu_id", "")
    if not menu_id:
        return {"error": "missing menu_id"}

    menu_cls = menu_registry.find_menu_class(menu_id)
    if menu_cls is None:
        return {"error": f"menu not found: {menu_id}"}

    menu_instance = menu_cls()
    execute_params = params.get("params", {})
    result = menu_instance.execute(execute_params, ctx)

    if not isinstance(result, MenuResult):
        return result if isinstance(result, dict) else {}

    result_dict: dict = {"action": result.action, "data": result.data}

    # 异步后续：根据 action 执行 stop/delete 并广播事件给其他客户端
    sm = ctx.managers.get("session_manager")
    session_id = result.data.get("session_id", "")

    if result.action == "session_created" and sm and session_id:
        session = sm.get(session_id)
        if session:
            await ctx.broadcast_event("session_created", _session_dict(session))

    elif result.action == "session_deleted" and sm and session_id:
        _close_channels_for_session(session_id, "session_deleted")
        await sm.stop(session_id)
        sm.delete(session_id)
        # 从 workspace.sessions 列表移除
        wm = ctx.managers.get("workspace_manager")
        if wm:
            ws = wm.get(ctx.workspace_id)
            if ws and session_id in ws.sessions:
                ws.sessions.remove(session_id)
                wm.update(ws)
        await ctx.broadcast_event("session_deleted", {"session_id": session_id})

    elif result.action == "session_deleted_batch" and sm:
        batch_ids = result.data.get("session_ids", [])
        wm = ctx.managers.get("workspace_manager")
        ws = wm.get(ctx.workspace_id) if wm else None
        for sid in batch_ids:
            _close_channels_for_session(sid, "session_deleted")
            await sm.stop(sid)
            sm.delete(sid)
            if ws and sid in ws.sessions:
                ws.sessions.remove(sid)
            await ctx.broadcast_event("session_deleted", {"session_id": sid})
        if ws and wm:
            wm.update(ws)

    return result_dict


# ---------------------------------------------------------------------------
# Session RPC handlers
# ---------------------------------------------------------------------------

@workspace_rpc.method("session.create")
async def handle_session_create(params: dict, ctx: RpcContext) -> dict:
    """创建 Session"""
    wm = ctx.managers.get("workspace_manager")
    sm = ctx.managers.get("session_manager")
    if not sm or not wm:
        return {"error": "managers not available"}

    ws = wm.get(ctx.workspace_id)
    if ws is None:
        return {"error": "workspace not found"}

    session_type = params.get("type", "")
    config = params.get("config")

    # 未指定类型时：空工作区默认 AgentSession，否则返回错误
    from mutbot.session import Session
    if not session_type:
        # 检查工作区是否为空（无 session）
        existing = sm.list_by_workspace(ws.id)
        if not existing:
            session_type = "mutbot.session.AgentSession"
        else:
            return {"error": "session type is required"}
    try:
        Session.get_session_class(session_type)
    except ValueError:
        return {"error": f"unknown session type: {session_type}"}

    # 将创建参数写入 config，on_create 中按需使用
    config = config or {}
    if params.get("rows"):
        config["rows"] = params["rows"]
    if params.get("cols"):
        config["cols"] = params["cols"]
    config.setdefault("cwd", ws.project_path)

    session = sm.create(ctx.workspace_id, session_type=session_type, config=config)
    ws.sessions.append(session.id)
    wm.update(ws)

    data = _session_dict(session)
    await ctx.broadcast_event("session_created", data)
    return data


@workspace_rpc.method("session.run_tool")
async def handle_session_run_tool(params: dict, ctx: RpcContext) -> dict:
    """在指定 session 中请求执行一个工具调用。

    params:
      - session_id (必填): 目标 session
      - tool (必填): 工具名称（如 "Config-llm"）
      - input (可选): 工具参数
    """
    sm = ctx.managers.get("session_manager")
    if not sm:
        return {"error": "session_manager not available"}

    session_id = params.get("session_id", "")
    tool_name = params.get("tool", "")
    tool_input = params.get("input", {})
    if not session_id or not tool_name:
        return {"error": "session_id and tool are required"}

    bridge = sm.get_bridge(session_id)
    if not bridge:
        return {"error": "session not running"}

    bridge.request_tool(tool_name, tool_input)
    return {"ok": True}


@workspace_rpc.method("session.run_setup")
async def handle_run_setup(params: dict, ctx: RpcContext) -> dict:
    """在指定 session 上触发 Config-llm 配置工具。

    params:
      - session_id: 目标 session（必须）

    确保 bridge 已启动后调用 request_tool("Config-llm")。
    """
    sm = ctx.managers.get("session_manager")
    if not sm:
        return {"error": "session manager not available"}

    session_id = params.get("session_id", "")
    if not session_id:
        return {"error": "session_id is required"}

    session = sm.get(session_id)
    if session is None:
        return {"error": f"session {session_id} not found"}

    # 确保 bridge 已启动
    loop = asyncio.get_running_loop()
    cm = _get_channel_manager()
    try:
        bridge = sm.start(
            session_id, loop, _make_channel_broadcast_fn(cm),
        )
    except Exception as exc:
        logger.exception("Failed to start bridge for setup session=%s", session_id)
        return {"error": str(exc)}

    bridge.request_tool("Config-llm")
    return {"ok": True, "session_id": session_id}


@workspace_rpc.method("session.types")
async def handle_session_types(params: dict, ctx: RpcContext) -> list[dict]:
    """返回可用的 Session 类型列表"""
    import mutobj
    from mutbot.session import Session, AgentSession

    result = []
    for cls in mutobj.discover_subclasses(Session):
        qualified = f"{cls.__module__}.{cls.__qualname__}"
        kind = _session_kind(qualified)
        label, icon = _session_type_display(qualified, cls)
        result.append({
            "type": qualified,
            "kind": kind,
            "label": label,
            "icon": icon,
            "is_agent": issubclass(cls, AgentSession),
        })
    return result


@workspace_rpc.method("session.list")
async def handle_session_list(params: dict, ctx: RpcContext) -> list[dict]:
    """列出 workspace 下的所有 Session，按 workspace.sessions 顺序返回"""
    sm = ctx.managers.get("session_manager")
    if not sm:
        return []
    workspace_id = params.get("workspace_id", ctx.workspace_id)
    # 按 workspace.sessions 列表顺序排列
    wm = ctx.managers.get("workspace_manager")
    ws = wm.get(workspace_id) if wm else None
    if ws and ws.sessions:
        order = {sid: idx for idx, sid in enumerate(ws.sessions)}
        all_sessions = sm.list_by_workspace(workspace_id)
        all_sessions.sort(key=lambda s: order.get(s.id, len(order)))
        return [_session_dict(s) for s in all_sessions]
    return [_session_dict(s) for s in sm.list_by_workspace(workspace_id)]


@workspace_rpc.method("session.get")
async def handle_session_get(params: dict, ctx: RpcContext) -> dict:
    """获取单个 Session"""
    sm = ctx.managers.get("session_manager")
    if not sm:
        return {"error": "session_manager not available"}
    session_id = params.get("session_id", "")
    session = sm.get(session_id)
    if session is None:
        return {"error": "session not found"}
    return _session_dict(session)


@workspace_rpc.method("session.messages")
async def handle_session_messages(params: dict, ctx: RpcContext) -> dict:
    """获取 Session 的持久化消息（序列化 Message[] blocks 格式，用于前端历史恢复）"""
    sm = ctx.managers.get("session_manager")
    if not sm:
        return {"error": "session_manager not available"}
    session_id = params.get("session_id", "")
    session = sm.get(session_id)
    if session is None:
        return {"error": "session not found"}

    # Agent 显示信息
    display_name = getattr(type(session), "display_name", "") or type(session).__name__
    agent_display: dict = {"name": display_name}
    avatar = session.config.get("avatar") if hasattr(session, "config") else None
    if avatar:
        agent_display["avatar"] = avatar

    # 从 agent.context.messages 获取，回退到磁盘
    from mutbot.web.serializers import serialize_message
    messages: list = []
    rt = sm.get_agent_runtime(session_id)
    if rt and rt.agent and rt.agent.context.messages:
        messages = [serialize_message(m) for m in rt.agent.context.messages]
    else:
        from mutbot.runtime import storage
        data = storage.load_session_metadata(session_id)
        if data:
            messages = data.get("messages", [])

    return {
        "session_id": session_id,
        "messages": messages,
        "total_tokens": getattr(session, "total_tokens", 0),
        "context_used": getattr(session, "context_used", 0),
        "context_window": getattr(session, "context_window", 0),
        "agent_display": agent_display,
    }


@workspace_rpc.method("session.stop")
async def handle_session_stop(params: dict, ctx: RpcContext) -> dict:
    """停止 Session"""
    sm = ctx.managers.get("session_manager")
    if not sm:
        return {"error": "session_manager not available"}
    session_id = params.get("session_id", "")
    await sm.stop(session_id)
    session = sm.get(session_id)
    data = _session_dict(session) if session else {"session_id": session_id}
    await ctx.broadcast_event("session_updated", data)
    return {"status": session.status if session else "stopped"}


@workspace_rpc.method("session.delete")
async def handle_session_delete(params: dict, ctx: RpcContext) -> dict:
    """删除 Session（先停止后删除，同步更新 workspace）"""
    sm = ctx.managers.get("session_manager")
    wm = ctx.managers.get("workspace_manager")
    if not sm:
        return {"error": "session_manager not available"}
    session_id = params.get("session_id", "")
    # Close associated channels before stopping
    _close_channels_for_session(session_id, "session_deleted")
    # Notify terminal clients before stop so they receive 0x04 while connections are live
    tm = ctx.managers.get("terminal_manager")
    if tm:
        session = sm.get(session_id)
        if session and isinstance(getattr(session, "config", None), dict):
            terminal_id = session.config.get("terminal_id")
            if terminal_id and tm.has(terminal_id):
                await tm.async_notify_exit(terminal_id)
    await sm.stop(session_id)
    if not sm.delete(session_id):
        return {"error": "session not found"}
    # 从 workspace.sessions 列表移除
    if wm:
        ws = wm.get(ctx.workspace_id)
        if ws and session_id in ws.sessions:
            ws.sessions.remove(session_id)
            wm.update(ws)
    await ctx.broadcast_event("session_deleted", {"session_id": session_id})
    return {"status": "deleted"}


@workspace_rpc.method("session.delete_batch")
async def handle_session_delete_batch(params: dict, ctx: RpcContext) -> dict:
    """批量删除 sessions。"""
    sm = ctx.managers.get("session_manager")
    wm = ctx.managers.get("workspace_manager")
    if not sm:
        return {"error": "session_manager not available"}
    session_ids = params.get("session_ids", [])
    if not session_ids:
        return {"error": "no session_ids provided"}
    tm = ctx.managers.get("terminal_manager")
    ws = wm.get(ctx.workspace_id) if wm else None
    for sid in session_ids:
        # Close associated channels before stopping
        _close_channels_for_session(sid, "session_deleted")
        # Notify terminal clients before stop
        if tm:
            session = sm.get(sid)
            if session and isinstance(getattr(session, "config", None), dict):
                terminal_id = session.config.get("terminal_id")
                if terminal_id and tm.has(terminal_id):
                    await tm.async_notify_exit(terminal_id)
        await sm.stop(sid)
        sm.delete(sid)
        if ws and sid in ws.sessions:
            ws.sessions.remove(sid)
        await ctx.broadcast_event("session_deleted", {"session_id": sid})
    if ws and wm:
        wm.update(ws)
    return {"status": "deleted", "count": len(session_ids)}


@workspace_rpc.method("session.update")
async def handle_session_update(params: dict, ctx: RpcContext) -> dict:
    """更新 Session 字段"""
    sm = ctx.managers.get("session_manager")
    if not sm:
        return {"error": "session_manager not available"}
    session_id = params.get("session_id", "")
    fields: dict[str, Any] = {}
    if "title" in params:
        fields["title"] = params["title"]
    if "config" in params:
        fields["config"] = params["config"]
    if "status" in params:
        fields["status"] = params["status"]
    if "model" in params:
        fields["model"] = params["model"]
    if not fields:
        return {"error": "no updatable fields"}
    session = sm.update(session_id, **fields)
    if session is None:
        return {"error": "session not found"}
    data = _session_dict(session)
    await ctx.broadcast_event("session_updated", data)
    return data


@workspace_rpc.method("session.restart")
async def handle_session_restart(params: dict, ctx: RpcContext) -> dict:
    """重启一个 TerminalSession：重建 PTY（全新开始）。

    用于用户点击 "Restart Terminal" 的场景：
    - 先终止残留的旧 PTY（如仍存在）
    - 清除 scrollback_b64（历史已通过 WS handler 展示给用户）
    - 调用 on_create() 重建全新 PTY
    - 返回新的 terminal_id 供前端 WebSocket 连接
    """
    sm = ctx.managers.get("session_manager")
    tm = ctx.managers.get("terminal_manager")
    if not sm:
        return {"error": "session_manager not available"}
    session_id = params.get("session_id", "")
    session = sm.get(session_id)
    if session is None:
        return {"error": "session not found"}
    from mutbot.session import TerminalSession as _TS
    if not isinstance(session, _TS):
        return {"error": "session is not a TerminalSession"}
    # Kill old terminal if still alive
    _close_channels_for_session(session_id, "session_restarted")
    if tm:
        old_term_id = session.config.get("terminal_id")
        if old_term_id and tm.has(old_term_id):
            await tm.async_notify_exit(old_term_id)
            tm.kill(old_term_id)
    # Start fresh: clear saved scrollback so on_create() creates a clean PTY
    session.scrollback_b64 = ""
    session.on_create(sm)
    sm._persist(session)
    new_term_id = session.config.get("terminal_id", "")
    data = _session_dict(session)
    await ctx.broadcast_event("session_updated", data)
    return {"session_id": session_id, "terminal_id": new_term_id}


# ---------------------------------------------------------------------------
# Config RPC handlers
# ---------------------------------------------------------------------------

@workspace_rpc.method("config.models")
async def handle_config_models(params: dict, ctx: RpcContext) -> dict:
    """返回所有已配置的模型列表"""
    from mutagent.provider import LLMProvider

    sm = ctx.managers.get("session_manager")
    if sm is None:
        return {"models": [], "default_model": ""}
    config = sm.config
    models = LLMProvider.list_models(config)
    default_model = config.get("default_model", default="")
    return {
        "models": [
            {"name": m["name"], "model_id": m["model_id"], "provider_name": m["provider_name"]}
            for m in models
        ],
        "default_model": default_model,
    }


# ---------------------------------------------------------------------------
# Workspace RPC handlers
# ---------------------------------------------------------------------------

@workspace_rpc.method("workspace.get")
async def handle_workspace_get(params: dict, ctx: RpcContext) -> dict:
    """获取 workspace 详情"""
    wm = ctx.managers.get("workspace_manager")
    if not wm:
        return {"error": "workspace_manager not available"}
    workspace_id = params.get("workspace_id", ctx.workspace_id)
    ws = wm.get(workspace_id)
    if ws is None:
        return {"error": "workspace not found"}
    return _workspace_dict(ws)


@workspace_rpc.method("workspace.update")
async def handle_workspace_update(params: dict, ctx: RpcContext) -> dict:
    """更新 workspace 字段（如 layout）"""
    wm = ctx.managers.get("workspace_manager")
    if not wm:
        return {"error": "workspace_manager not available"}
    workspace_id = params.get("workspace_id", ctx.workspace_id)
    ws = wm.get(workspace_id)
    if ws is None:
        return {"error": "workspace not found"}
    if "layout" in params:
        ws.layout = params["layout"]
    wm.update(ws)
    return _workspace_dict(ws)


@workspace_rpc.method("workspace.reorder_sessions")
async def handle_reorder_sessions(params: dict, ctx: RpcContext) -> dict:
    """更新 workspace 中的 session 排列顺序。"""
    wm = ctx.managers.get("workspace_manager")
    if not wm:
        return {"error": "workspace_manager not available"}
    ws = wm.get(ctx.workspace_id)
    if ws is None:
        return {"error": "workspace not found"}
    new_order = params.get("session_ids", [])
    # 校验 ID 集合一致
    if set(new_order) != set(ws.sessions):
        return {"error": "session_ids mismatch"}
    ws.sessions = new_order
    wm.update(ws)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Terminal RPC handlers
# ---------------------------------------------------------------------------

@workspace_rpc.method("terminal.create")
async def handle_terminal_create(params: dict, ctx: RpcContext) -> dict:
    """创建终端"""
    wm = ctx.managers.get("workspace_manager")
    tm = ctx.managers.get("terminal_manager")
    if not wm or not tm:
        return {"error": "managers not available"}
    ws = wm.get(ctx.workspace_id)
    if ws is None:
        return {"error": "workspace not found"}
    rows = params.get("rows", 24)
    cols = params.get("cols", 80)
    term = tm.create(ctx.workspace_id, rows, cols, cwd=ws.project_path)
    data = _terminal_dict(term)
    await ctx.broadcast_event("terminal_created", data)
    return data


@workspace_rpc.method("terminal.list")
async def handle_terminal_list(params: dict, ctx: RpcContext) -> list[dict]:
    """列出 workspace 下的所有终端"""
    tm = ctx.managers.get("terminal_manager")
    if not tm:
        return []
    return [_terminal_dict(t) for t in tm.list_by_workspace(ctx.workspace_id)]


@workspace_rpc.method("terminal.delete")
async def handle_terminal_delete(params: dict, ctx: RpcContext) -> dict:
    """删除终端"""
    tm = ctx.managers.get("terminal_manager")
    if not tm:
        return {"error": "terminal_manager not available"}
    term_id = params.get("term_id", "")
    if not tm.has(term_id):
        return {"error": "terminal not found"}
    await tm.async_notify_exit(term_id)
    tm.kill(term_id)
    await ctx.broadcast_event("terminal_deleted", {"term_id": term_id})
    return {"status": "killed"}


# ---------------------------------------------------------------------------
# File RPC handlers
# ---------------------------------------------------------------------------

@workspace_rpc.method("file.read")
async def handle_file_read(params: dict, ctx: RpcContext) -> dict:
    """读取文件内容"""
    wm = ctx.managers.get("workspace_manager")
    if not wm:
        return {"error": "workspace_manager not available"}
    ws = wm.get(ctx.workspace_id)
    if ws is None:
        return {"error": "workspace not found"}

    file_path = params.get("path", "")
    if not file_path:
        return {"error": "missing path"}

    project = Path(ws.project_path).resolve()
    target = (project / file_path).resolve()
    if not str(target).startswith(str(project)):
        return {"error": "path traversal not allowed"}
    if not target.is_file():
        return {"error": "file not found"}

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"error": str(exc)}

    ext = target.suffix.lower()
    language = _LANG_MAP.get(ext, "plaintext")
    return {"path": str(target.relative_to(project)), "content": content, "language": language}


# ---------------------------------------------------------------------------
# Log RPC handlers
# ---------------------------------------------------------------------------

@workspace_rpc.method("log.query")
async def handle_log_query(params: dict, ctx: RpcContext) -> dict:
    """查询内存日志"""
    store = _get_log_store()
    if store is None:
        return {"entries": [], "total": 0}
    pattern = params.get("pattern", "")
    level = params.get("level", "DEBUG")
    limit = params.get("limit", 50)
    entries = store.query(pattern=pattern, level=level, limit=limit)
    return {
        "total": store.count(),
        "returned": len(entries),
        "entries": [
            {
                "timestamp": e.timestamp,
                "level": e.level,
                "logger": e.logger_name,
                "message": e.message,
            }
            for e in entries
        ],
    }


def _get_managers():
    """Lazy import of global managers from server module."""
    from mutbot.web.server import workspace_manager, session_manager
    return workspace_manager, session_manager


def _get_log_store():
    from mutbot.web.server import log_store
    return log_store


def _get_terminal_manager():
    from mutbot.web.server import terminal_manager
    return terminal_manager


def _get_channel_manager():
    from mutbot.web.server import channel_manager
    return channel_manager


def _workspace_dict(ws) -> dict[str, Any]:
    return {
        "id": ws.id,
        "name": ws.name,
        "project_path": ws.project_path,
        "sessions": ws.sessions,
        "layout": ws.layout,
        "created_at": ws.created_at,
        "updated_at": ws.updated_at,
        "last_accessed_at": ws.last_accessed_at,
    }


def _session_dict(s) -> dict[str, Any]:
    # kind: 从类名推导短类型名，供前端 switch/display 使用
    kind = _session_kind(s.type)
    # icon: 用户自定义 > 类声明 > 空串（前端用 kind 回退）
    icon = s.config.get("icon") or getattr(type(s), "display_icon", "") or ""
    d: dict[str, Any] = {
        "id": s.id,
        "workspace_id": s.workspace_id,
        "title": s.title,
        "type": s.type,
        "kind": kind,
        "icon": icon,
        "status": s.status,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "config": s.config,
    }
    # AgentSession 额外字段
    from mutbot.session import AgentSession
    if isinstance(s, AgentSession):
        d["model"] = s.model
    return d


def _session_kind(session_type: str) -> str:
    """从全限定类型名推导短类型名。"""
    # 从类名推导 ("mutbot.builtins.guide.GuideSession" → "guide")
    parts = session_type.rsplit(".", 1)
    name = parts[-1] if parts else session_type
    if name.endswith("Session"):
        name = name[:-7]
    return name.lower()


def _session_type_display(qualified: str, cls: type) -> tuple[str, str]:
    """获取 Session 类型的 (显示名, 图标)。"""
    name = getattr(cls, "display_name", "") or ""
    icon = getattr(cls, "display_icon", "") or ""
    if not name:
        # 回退：从类名推导
        raw = cls.__name__
        if raw.endswith("Session"):
            raw = raw[:-7]
        name = raw
    if not icon:
        icon = _session_kind(qualified)
    return (name, icon)


# ---------------------------------------------------------------------------
# fe_logger — for frontend log forwarding via channel
# ---------------------------------------------------------------------------

fe_logger = logging.getLogger("mutbot.frontend")


def _terminal_dict(t) -> dict[str, Any]:
    return {
        "id": t.id,
        "workspace_id": t.workspace_id,
        "rows": t.rows,
        "cols": t.cols,
        "alive": t.alive,
    }


# ---------------------------------------------------------------------------
# Log streaming WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """Stream new log entries to client in real-time."""
    await websocket.accept()
    logger.info("Log WS connected")

    store = _get_log_store()
    if store is None:
        await websocket.close(code=4500, reason="log store not available")
        return

    # Start from current end (no history replay)
    cursor = store.count()

    try:
        while True:
            await asyncio.sleep(0.2)
            current_count = store.count()
            if current_count > cursor:
                # Fetch new entries
                new_entries = store.query(pattern="", level="DEBUG", limit=current_count - cursor)
                # query returns newest-first, reverse to send oldest-first
                for e in reversed(new_entries):
                    await websocket.send_json({
                        "type": "log",
                        "timestamp": e.timestamp,
                        "level": e.level,
                        "logger": e.logger_name,
                        "message": e.message,
                    })
                cursor = current_count
    except WebSocketDisconnect:
        logger.info("Log WS disconnected")
    except Exception:
        logger.exception("Log WS error")


# ---------------------------------------------------------------------------
# File read endpoint
# ---------------------------------------------------------------------------

_LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescriptreact",
    ".jsx": "javascriptreact", ".json": "json", ".html": "html", ".css": "css",
    ".md": "markdown", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".sh": "shell", ".bash": "shell", ".sql": "sql", ".xml": "xml",
    ".rs": "rust", ".go": "go", ".java": "java", ".c": "c", ".cpp": "cpp",
    ".h": "c", ".hpp": "cpp", ".rb": "ruby", ".php": "php",
}


# ---------------------------------------------------------------------------
# App-level WebSocket RPC endpoint (全局，无需指定 workspace)
# ---------------------------------------------------------------------------

@router.websocket("/ws/app")
async def websocket_app(websocket: WebSocket):
    """全局 WebSocket：工作区列表、创建工作区、目录浏览。

    消息格式与 /ws/workspace/{id} 一致（JSON-RPC）。
    """
    # Origin 校验
    origin = websocket.headers.get("origin")
    if not _check_ws_origin(origin):
        await websocket.close(code=4403, reason="origin not allowed")
        return

    wm, sm = _get_managers()
    await websocket.accept()
    logger.info("App WS connected (origin=%s)", origin or "none")

    # 推送 welcome 事件：应用状态（版本、setup_required 等）
    import mutbot
    _cfg = websocket.app.state.config
    await websocket.send_json({
        "type": "event",
        "event": "welcome",
        "data": {
            "version": mutbot.__version__,
            "setup_required": not bool(_cfg.get("providers")),
        },
    })

    async def broadcast(data: dict) -> None:
        pass  # app 级连接不需要广播

    context = RpcContext(
        workspace_id="",
        broadcast=broadcast,
        managers={"workspace_manager": wm, "session_manager": sm, "config": _cfg},
    )

    try:
        while True:
            raw = await websocket.receive_json()
            response = await app_rpc.dispatch(raw, context)
            if response is not None:
                await websocket.send_json(response)
    except WebSocketDisconnect:
        logger.info("App WS disconnected")
    except Exception:
        logger.exception("App WS error")


# ---------------------------------------------------------------------------
# Workspace WebSocket RPC endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/workspace/{workspace_id}")
async def websocket_workspace(websocket: WebSocket, workspace_id: str):
    """统一 Workspace WebSocket：承载 RPC 调用、事件推送和 Channel 多路复用。

    支持 Text Frame（JSON）和 Binary Frame 混合传输。
    通过 client_id + last_seq 支持断线重连时的消息恢复。
    """
    wm, sm = _get_managers()
    ws = wm.get(workspace_id)
    if ws is None:
        await websocket.close(code=4004, reason="workspace not found")
        return

    wm.touch_accessed(ws)

    # --- 解析重连参数 ---
    client_id = websocket.query_params.get("client_id", "")
    last_seq_str = websocket.query_params.get("last_seq")
    last_seq = int(last_seq_str) if last_seq_str is not None else None

    await websocket.accept()

    loop = asyncio.get_running_loop()
    cm = _get_channel_manager()

    # --- 确保 SessionManager 能从 Agent 线程广播事件 ---
    if sm and not sm._broadcast_fn:
        async def _sm_broadcast(ws_id: str, data: dict) -> None:
            _broadcast_to_workspace(ws_id, data)
        sm.set_broadcast(loop, _sm_broadcast)

    # --- Client 匹配 / 创建 ---
    resumed = False
    client: Client | None = None

    if client_id and last_seq is not None:
        client = _clients.get(client_id)
        if client and client.state == "buffering" and client.workspace_id == workspace_id:
            if client.resume(websocket, last_seq):
                resumed = True
                logger.info(
                    "Workspace WS resumed: client=%s, last_seq=%d",
                    client_id, last_seq,
                )
            else:
                # last_seq 不可覆盖 → 完全重连
                client.reset_for_fresh_connection(websocket)
                logger.info(
                    "Workspace WS full reconnect (seq out of range): client=%s",
                    client_id,
                )
        else:
            client = None  # 不匹配 → 当新连接处理

    if client is None:
        # 新连接
        if not client_id:
            import uuid
            client_id = str(uuid.uuid4())
        client = Client(client_id, workspace_id, websocket, loop=loop)
        client.start()

        # 注册过期回调
        def _on_client_expire(c: Client) -> None:
            cm.close_all_for_client(c)
            _clients.pop(c.client_id, None)
            ws_clients = _workspace_clients.get(c.workspace_id)
            if ws_clients:
                ws_clients.discard(c)
                if not ws_clients:
                    del _workspace_clients[c.workspace_id]

        client.on_expire(_on_client_expire)
        _clients[client_id] = client
        logger.info("Workspace WS connected: client=%s, workspace=%s", client_id, workspace_id)

    # --- 发送 welcome ---
    welcome: dict[str, Any] = {"type": "welcome", "resumed": resumed}
    if resumed:
        welcome["last_seq"] = client.recv_count
        # 重发缺失消息
        replay_msgs = client.get_replay_messages(last_seq)
        try:
            await websocket.send_json(welcome)
            for frame_type, data in replay_msgs:
                if frame_type == "json":
                    await websocket.send_json(data)
                else:
                    await websocket.send_bytes(data)
        except Exception:
            logger.exception("Failed to send welcome/replay")
            client.enter_buffering()
            return
    else:
        try:
            await websocket.send_json(welcome)
        except Exception:
            logger.exception("Failed to send welcome")
            return

    # --- 注册到 workspace clients 索引 ---
    _workspace_clients.setdefault(workspace_id, set()).add(client)

    # 新连接推送 config_changed
    if not resumed:
        client.send_json(make_event("config_changed", {"reason": "connect"}))

    # 新连接 flush pending events
    if not resumed:
        for event in _pop_pending_events(workspace_id):
            client.send_json(event)

    # --- RPC context ---
    async def broadcast(data: dict) -> None:
        _broadcast_to_workspace(workspace_id, data)

    context = RpcContext(
        workspace_id=workspace_id,
        broadcast=broadcast,
        managers={
            "workspace_manager": wm,
            "session_manager": sm,
            "terminal_manager": _get_terminal_manager(),
            "channel_manager": cm,
            "config": websocket.app.state.config,
        },
        sender_ws=websocket,
    )

    # --- 注册 channel.open / channel.close RPC ---
    _ensure_channel_rpc_registered()

    # --- 消息循环 ---
    try:
        while True:
            msg = await websocket.receive()
            ws_type = msg.get("type", "")

            if ws_type == "websocket.receive":
                if "text" in msg:
                    # --- JSON (Text Frame) ---
                    import json as _json
                    try:
                        raw = _json.loads(msg["text"])
                    except Exception:
                        logger.warning("Invalid JSON in WS frame", exc_info=True)
                        continue

                    msg_type = raw.get("type", "")

                    # 控制消息
                    if msg_type == "ack":
                        client.on_peer_ack(raw.get("ack", 0))
                        client.on_control_received()
                        continue

                    # 内容消息 → 递增计数
                    client.on_content_received()

                    ch = raw.get("ch", 0)
                    if ch == 0:
                        # Workspace 级消息 → RPC dispatch
                        context._post_send = None
                        response = await workspace_rpc.dispatch(raw, context)
                        if response is not None:
                            client.send_json(response)
                        if context._post_send is not None:
                            context._post_send()
                    else:
                        # Channel 消息 → 路由到对应 channel handler
                        channel = cm.get_channel(ch)
                        if channel:
                            await _handle_channel_json(channel, raw, sm, cm, loop, context)

                elif "bytes" in msg:
                    # --- Binary Frame ---
                    data = msg["bytes"]
                    if not data:
                        continue
                    client.on_content_received()
                    try:
                        ch_id, consumed = decode_varint(data)
                    except ValueError:
                        logger.warning("Invalid varint in binary frame", exc_info=True)
                        continue
                    channel = cm.get_channel(ch_id)
                    if channel:
                        await _handle_channel_binary(channel, data[consumed:])

            elif ws_type == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        logger.info("Workspace WS disconnected: client=%s", client_id)
    except Exception:
        logger.exception("Workspace WS error: client=%s", client_id)
    finally:
        ws_clients = _workspace_clients.get(workspace_id)
        if ws_clients:
            ws_clients.discard(client)
            if not ws_clients:
                del _workspace_clients[workspace_id]
        client.enter_buffering()


# ---------------------------------------------------------------------------
# Channel message handlers
# ---------------------------------------------------------------------------


async def _handle_channel_json(
    channel, raw: dict, sm, cm, loop, context: RpcContext,
) -> None:
    """处理频道级 JSON 消息。"""
    session_id = channel.session_id
    if not session_id:
        return

    from mutbot.session import AgentSession
    session = sm.get(session_id)
    if session is None:
        return

    msg_type = raw.get("type", "")

    if not isinstance(session, AgentSession):
        return

    if msg_type == "message":
        text = raw.get("text", "")
        data = raw.get("data")
        if text:
            bridge = sm.get_bridge(session_id)
            if bridge is None:
                was_inactive = session.status == ""
                try:
                    broadcast_fn = _make_channel_broadcast_fn(cm)
                    bridge = sm.start(session_id, loop, broadcast_fn)
                except Exception as exc:
                    logger.exception("Failed to start agent: session=%s", session_id)
                    await sm.stop(session_id)
                    channel.enqueue_json({"type": "error", "error": str(exc)})
                    return
                if was_inactive:
                    _broadcast_to_workspace(
                        session.workspace_id,
                        make_event("session_updated", _session_dict(session)),
                    )
            bridge.send_message(text, data)
    elif msg_type == "cancel":
        bridge = sm.get_bridge(session_id)
        if bridge:
            await bridge.cancel()
    elif msg_type == "run_tool":
        tool_name = raw.get("tool", "")
        bridge = sm.get_bridge(session_id)
        if tool_name and bridge:
            bridge.request_tool(tool_name, raw.get("input", {}))
    elif msg_type == "ui_event":
        from mutbot.ui import deliver_event
        from mutbot.ui.events import UIEvent
        context_id = raw.get("context_id", "")
        if context_id:
            event = UIEvent(
                type=raw.get("event_type", ""),
                data=raw.get("data", {}),
                source=raw.get("source"),
                context_id=context_id,
            )
            deliver_event(context_id, event)
    elif msg_type == "log":
        level = raw.get("level", "debug")
        message = raw.get("message", "")
        log_fn = getattr(fe_logger, level, fe_logger.debug)
        log_fn("[%s] %s", session_id[:8], message)
    elif msg_type == "stop":
        await sm.stop(session_id)


async def _handle_channel_binary(channel, payload: bytes) -> None:
    """处理频道级 Binary 消息（Terminal I/O）。"""
    from mutbot.session import TerminalSession
    session_id = channel.session_id
    if not session_id:
        return

    tm = _get_terminal_manager()
    if tm is None:
        return

    _, sm = _get_managers()
    session = sm.get(session_id) if sm else None
    if not isinstance(session, TerminalSession):
        return

    term_id = session.config.get("terminal_id", "")
    if not term_id or not tm.has(term_id):
        return

    if len(payload) < 1:
        return

    msg_type = payload[0]
    if msg_type == 0x00:
        # Terminal input
        tm.write(term_id, payload[1:])
    elif msg_type == 0x02 and len(payload) >= 5:
        # Resize: 2B rows + 2B cols (big-endian)
        rows = int.from_bytes(payload[1:3], "big")
        cols = int.from_bytes(payload[3:5], "big")
        client_id = str(channel.client.client_id)
        tm.resize(term_id, rows, cols, client_id=client_id)


# ---------------------------------------------------------------------------
# Channel broadcast function (replaces old ConnectionManager.broadcast)
# ---------------------------------------------------------------------------


def _make_channel_broadcast_fn(cm):
    """创建基于 ChannelManager 的 broadcast_fn（AgentBridge 使用）。

    签名与旧 broadcast_fn 一致: async (session_id, data) -> None
    """
    async def broadcast_fn(session_id: str, data: dict) -> None:
        channels = cm.get_channels_for_session(session_id)
        for channel in channels:
            channel.enqueue_json(data)
    return broadcast_fn


# ---------------------------------------------------------------------------
# Channel RPC handlers (channel.open / channel.close)
# ---------------------------------------------------------------------------

_channel_rpc_registered = False


def _ensure_channel_rpc_registered() -> None:
    global _channel_rpc_registered
    if _channel_rpc_registered:
        return
    _channel_rpc_registered = True

    @workspace_rpc.method("channel.open")
    async def handle_channel_open(params: dict, ctx: RpcContext) -> dict:
        target = params.get("target", "")
        session_id = params.get("session_id")
        cm = ctx.managers.get("channel_manager")
        sm = ctx.managers.get("session_manager")

        if not target:
            raise ValueError("missing target")
        if target == "session" and not session_id:
            raise ValueError("missing session_id")

        # 查找发送者的 Client
        client = _find_client_by_ws(ctx.sender_ws)
        if client is None:
            raise ValueError("client not found")

        channel = cm.open(client, target, session_id=session_id)

        # 通过 post_send 延后 attach，确保 rpc_result 先入队
        if target == "session" and session_id and sm:
            from mutbot.session import TerminalSession, AgentSession
            session = sm.get(session_id)
            if isinstance(session, TerminalSession):
                def _post_attach():
                    _attach_terminal_channel(channel, session, sm)
                ctx._post_send = _post_attach
            elif isinstance(session, AgentSession):
                _ensure_agent_broadcast(session_id, sm, cm, channel)

        return {"ch": channel.ch}

    @workspace_rpc.method("channel.close")
    async def handle_channel_close(params: dict, ctx: RpcContext) -> dict:
        ch = params.get("ch", 0)
        cm = ctx.managers.get("channel_manager")
        channel = cm.close(ch)
        if channel:
            # Terminal → detach
            _detach_terminal_channel(channel)
        return {"ok": True}


def _find_client_by_ws(ws) -> Client | None:
    """查找拥有指定 WebSocket 的 Client。"""
    for client in _clients.values():
        if client.ws is ws:
            return client
    return None


def _attach_terminal_channel(channel, session, sm, loop=None) -> None:
    """打开 terminal channel 时自动 attach + scrollback replay。

    全同步操作（enqueue_binary = put_nowait），可作为 post_send 回调直接调用。
    """
    term_id = session.config.get("terminal_id", "")
    tm = _get_terminal_manager()
    if not tm or not term_id:
        return

    if not tm.has(term_id):
        # Terminal 已死 → 发送保存的 scrollback + 退出信号
        scrollback_data = b""
        if session.scrollback_b64:
            import base64
            try:
                scrollback_data = base64.b64decode(session.scrollback_b64)
            except Exception:
                logger.warning("scrollback decode failed", exc_info=True)
        if scrollback_data:
            channel.enqueue_binary(bytes([0x01]) + b"\x1b[0m" + scrollback_data)
        channel.enqueue_binary(bytes([0x03]))  # scrollback done
        channel.enqueue_binary(bytes([0x04]))  # process exited
        return

    # 发送 scrollback
    scrollback = tm.get_scrollback(term_id)
    if scrollback:
        channel.enqueue_binary(bytes([0x01]) + b"\x1b[0m" + scrollback)

    # 检查进程是否已退出
    ts = tm.get(term_id)
    if ts is None or not ts.alive:
        exit_code = ts.exit_code if ts else None
        channel.enqueue_binary(tm._make_exit_payload(exit_code))
        return

    # scrollback replay 完成
    channel.enqueue_binary(bytes([0x03]))

    # Attach — terminal 读线程通过 channel.enqueue_binary 推送输出
    client_id = channel.client.client_id

    def on_output(payload: bytes) -> None:
        channel.enqueue_binary(payload)

    if loop is None:
        loop = asyncio.get_running_loop()
    tm.attach(term_id, client_id, on_output, loop)


def _close_channels_for_session(session_id: str, reason: str) -> None:
    """关闭指定 session 的所有 channel 并推送 channel.closed 事件。

    用于 session 删除、终端删除等场景的被动关闭通知。
    """
    cm = _get_channel_manager()
    if cm is None:
        return
    channels = cm.get_channels_for_session(session_id)
    for channel in channels:
        _detach_terminal_channel(channel)
        cm.close(channel.ch)
        channel.client.send_json({
            "type": "event",
            "event": "channel.closed",
            "closed_ch": channel.ch,
            "reason": reason,
        })


def _detach_terminal_channel(channel) -> None:
    """关闭 terminal channel 时自动 detach。"""
    if not channel.session_id:
        return
    _, sm = _get_managers()
    if not sm:
        return
    from mutbot.session import TerminalSession
    session = sm.get(channel.session_id)
    if not isinstance(session, TerminalSession):
        return
    term_id = session.config.get("terminal_id", "")
    tm = _get_terminal_manager()
    if tm and term_id:
        tm.detach(term_id, channel.client.client_id)


def _ensure_agent_broadcast(session_id: str, sm, cm, channel) -> None:
    """确保 agent bridge 使用 channel-based broadcast。"""
    bridge = sm.get_bridge(session_id)
    if bridge is not None:
        # 已有 bridge → 更新其 broadcast_fn
        broadcast_fn = _make_channel_broadcast_fn(cm)
        bridge.broadcast_fn = broadcast_fn
