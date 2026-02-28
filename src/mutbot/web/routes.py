"""API routes — REST endpoints and WebSocket handler."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mutbot.web.connection import ConnectionManager
from mutbot.web.rpc import RpcDispatcher, RpcContext, make_event
from mutbot.runtime.menu_impl import menu_registry
from mutbot.menu import MenuResult

logger = logging.getLogger(__name__)

router = APIRouter()
connection_manager = ConnectionManager()
workspace_connection_manager = ConnectionManager()

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

    # 未指定类型时：空工作区默认 GuideSession，否则返回错误
    from mutbot.session import (
        Session, TerminalSession, DocumentSession,
    )
    if not session_type:
        # 检查工作区是否为空（无 session）
        existing = sm.list_by_workspace(ws.id)
        if not existing:
            session_type = "mutbot.builtins.guide.GuideSession"
        else:
            return {"error": "session type is required"}
    try:
        session_cls = Session.get_session_class(session_type)
    except ValueError:
        return {"error": f"unknown session type: {session_type}"}

    # terminal 类型需创建 PTY
    if issubclass(session_cls, TerminalSession):
        tm = ctx.managers.get("terminal_manager")
        if tm is None:
            return {"error": "terminal manager not available"}
        rows = params.get("rows", 24)
        cols = params.get("cols", 80)
        term = tm.create(ctx.workspace_id, rows, cols, cwd=ws.project_path)
        config = config or {}
        config["terminal_id"] = term.id

    # document 类型生成默认文件路径
    if issubclass(session_cls, DocumentSession):
        import time
        config = config or {}
        config.setdefault("file_path", f"untitled-{int(time.time() * 1000)}.md")

    session = sm.create(ctx.workspace_id, session_type=session_type, config=config)
    # TerminalSession: PTY 创建后设置 running 状态
    if issubclass(session_cls, TerminalSession):
        session.status = "running"
        sm._persist(session)
    ws.sessions.append(session.id)
    wm.update(ws)

    data = _session_dict(session)
    await ctx.broadcast_event("session_created", data)
    return data


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
    """获取 Session 的持久化 chat_messages（用于前端历史恢复）"""
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

    # 优先从内存中的 session 对象获取 chat_messages
    from mutbot.session import AgentSession
    chat_messages: list = []
    if isinstance(session, AgentSession) and session.chat_messages:
        chat_messages = session.chat_messages
    else:
        # 回退到磁盘
        from mutbot.runtime import storage
        data = storage.load_session_metadata(session_id)
        if data:
            chat_messages = data.get("chat_messages", [])

    return {
        "session_id": session_id,
        "chat_messages": chat_messages,
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
    return {"status": "stopped"}


@workspace_rpc.method("session.delete")
async def handle_session_delete(params: dict, ctx: RpcContext) -> dict:
    """删除 Session（先停止后删除，同步更新 workspace）"""
    sm = ctx.managers.get("session_manager")
    wm = ctx.managers.get("workspace_manager")
    if not sm:
        return {"error": "session_manager not available"}
    session_id = params.get("session_id", "")
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
    ws = wm.get(ctx.workspace_id) if wm else None
    for sid in session_ids:
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


# ---------------------------------------------------------------------------
# Config RPC handlers
# ---------------------------------------------------------------------------

@workspace_rpc.method("config.models")
async def handle_config_models(params: dict, ctx: RpcContext) -> dict:
    """返回所有已配置的模型列表"""
    from mutbot.runtime.config import load_mutbot_config
    config = load_mutbot_config()
    models = config.get_all_models()
    default_model = config.get("default_model", "")
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
# WebSocket handler
# ---------------------------------------------------------------------------

fe_logger = logging.getLogger("mutbot.frontend")


async def _broadcast_connection_count(session_id: str) -> None:
    """Broadcast current connection count to all clients of a session."""
    count = len(connection_manager.get_connections(session_id))
    await connection_manager.broadcast(
        session_id, {"type": "connection_count", "count": count}
    )


@router.websocket("/ws/session/{session_id}")
async def websocket_session(websocket: WebSocket, session_id: str):
    _, sm = _get_managers()
    session = sm.get(session_id)
    if session is None:
        await websocket.close(code=4004, reason="session not found")
        return

    await connection_manager.connect(session_id, websocket)
    logger.info("WS connected: session=%s (status=%s)", session_id, session.status)

    # Broadcast updated connection count
    await _broadcast_connection_count(session_id)

    loop = asyncio.get_running_loop()
    bridge = None

    # active session → 立即启动 agent bridge
    # stopped/empty status session → 延迟到用户发消息时再启动
    if session.status not in ("", "stopped"):
        try:
            bridge = sm.start(session_id, loop, connection_manager.broadcast)
        except Exception as exc:
            logger.exception("Failed to start agent for session=%s", session_id)
            await sm.stop(session_id)
            await websocket.send_json({"type": "error", "error": str(exc)})
            connection_manager.disconnect(session_id, websocket)
            await websocket.close(code=4500, reason="agent start failed")
            return

    try:
        while True:
            raw = await websocket.receive_json()
            msg_type = raw.get("type", "")
            if msg_type == "message":
                text = raw.get("text", "")
                data = raw.get("data")
                if text:
                    # 延迟启动：非活跃 session 在用户发消息时激活
                    if bridge is None:
                        was_inactive = session.status in ("", "stopped")
                        try:
                            bridge = sm.start(session_id, loop, connection_manager.broadcast)
                        except Exception as exc:
                            logger.exception("Failed to start agent for session=%s", session_id)
                            await sm.stop(session_id)
                            await websocket.send_json({"type": "error", "error": str(exc)})
                            break
                        # 广播 session 重新激活事件
                        if was_inactive:
                            await workspace_connection_manager.broadcast(
                                session.workspace_id,
                                make_event("session_updated", _session_dict(session)),
                            )
                    bridge.send_message(text, data)
            elif msg_type == "cancel":
                if bridge:
                    await bridge.cancel()
            elif msg_type == "log":
                # Frontend log forwarding
                level = raw.get("level", "debug")
                message = raw.get("message", "")
                log_fn = getattr(fe_logger, level, fe_logger.debug)
                log_fn("[%s] %s", session_id[:8], message)
            elif msg_type == "stop":
                await sm.stop(session_id)
                break
    except WebSocketDisconnect:
        logger.info("WS disconnected: session=%s", session_id)
    except Exception:
        logger.exception("WS error: session=%s", session_id)
    finally:
        connection_manager.disconnect(session_id, websocket)
        # Broadcast updated connection count after disconnect
        try:
            await _broadcast_connection_count(session_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Terminal WebSocket handler
# ---------------------------------------------------------------------------

def _terminal_dict(t) -> dict[str, Any]:
    return {
        "id": t.id,
        "workspace_id": t.workspace_id,
        "rows": t.rows,
        "cols": t.cols,
        "alive": t.alive,
    }


@router.websocket("/ws/terminal/{term_id}")
async def websocket_terminal(websocket: WebSocket, term_id: str):
    """Binary WebSocket for terminal I/O.

    Protocol:
    - Client→Server: 0x00 + input bytes
    - Server→Client: 0x01 + output bytes
    - Client→Server: 0x02 + 2B rows (big-endian) + 2B cols (big-endian)
    - Server→Client: 0x03   scrollback replay complete

    PTY survives WebSocket disconnects.  The client can reconnect and
    receive the scrollback buffer to restore the screen.
    """
    tm = _get_terminal_manager()
    await websocket.accept()
    if tm is None or not tm.has(term_id):
        await websocket.close(code=4004, reason="terminal not found")
        return
    logger.info("Terminal WS connected: term=%s", term_id)

    # Read optional terminal dimensions from query params for immediate
    # resize after scrollback replay (avoids cursor position mismatch).
    rows_param = int(websocket.query_params.get("rows", "0"))
    cols_param = int(websocket.query_params.get("cols", "0"))

    loop = asyncio.get_running_loop()

    # Send scrollback BEFORE attaching, so live output from the reader
    # thread doesn't arrive before the historical replay.
    scrollback = tm.get_scrollback(term_id)
    if scrollback:
        # Prepend attribute reset to handle truncated escape sequences
        # at the scrollback buffer boundary.
        try:
            await websocket.send_bytes(b"\x01\x1b[0m" + scrollback)
        except Exception:
            logger.debug("Failed to send scrollback for term=%s", term_id)

    # Signal scrollback replay is complete — client can unmute input
    try:
        await websocket.send_bytes(b"\x03")
    except Exception:
        pass

    # Immediately sync PTY dimensions to match the connecting client,
    # so the shell redraws its prompt at the correct cursor position.
    if rows_param > 0 and cols_param > 0:
        tm.resize(term_id, rows_param, cols_param)

    # Bind WebSocket → TerminalManager via callback
    client_id = id(websocket)
    tm.attach(term_id, str(client_id), websocket.send_bytes, loop)

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_bytes(), timeout=2.0)
            except asyncio.TimeoutError:
                # Periodic alive check — fallback for process exit detection
                session = tm.get(term_id)
                if session is None or not session.alive:
                    exit_code = session.exit_code if session else None
                    payload = tm._make_exit_payload(exit_code)
                    try:
                        await websocket.send_bytes(payload)
                    except Exception:
                        pass
                    break
                continue

            if len(raw) < 1:
                continue
            msg_type = raw[0]
            if msg_type == 0x00:
                # Terminal input
                tm.write(term_id, raw[1:])
            elif msg_type == 0x02 and len(raw) >= 5:
                # Resize: 2B rows + 2B cols (big-endian)
                rows = int.from_bytes(raw[1:3], "big")
                cols = int.from_bytes(raw[3:5], "big")
                tm.resize(term_id, rows, cols)
    except WebSocketDisconnect:
        logger.info("Terminal WS disconnected: term=%s", term_id)
    except Exception:
        logger.exception("Terminal WS error: term=%s", term_id)
    finally:
        # Detach only — PTY stays alive for reconnection
        tm.detach(term_id, str(client_id))


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

    # 推送 welcome 事件：应用状态（setup_required 等）
    from mutbot.runtime.config import load_mutbot_config
    _cfg = load_mutbot_config()
    await websocket.send_json({
        "type": "event",
        "event": "welcome",
        "data": {
            "setup_required": not bool(_cfg.get("providers")),
        },
    })

    async def broadcast(data: dict) -> None:
        pass  # app 级连接不需要广播

    context = RpcContext(
        workspace_id="",
        broadcast=broadcast,
        managers={"workspace_manager": wm},
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
    """Workspace 级 WebSocket：承载 RPC 调用和服务端事件推送。

    消息格式：
    - 请求: { "type": "rpc", "id": str, "method": str, "params": dict }
    - 响应: { "type": "rpc_result", "id": str, "result": Any }
    - 错误: { "type": "rpc_error", "id": str, "error": { "code": int, "message": str } }
    - 推送: { "type": "event", "event": str, "data": dict }
    """
    wm, sm = _get_managers()
    ws = wm.get(workspace_id)
    if ws is None:
        await websocket.close(code=4004, reason="workspace not found")
        return

    wm.touch_accessed(ws)
    await workspace_connection_manager.connect(workspace_id, websocket)
    logger.info("Workspace WS connected: workspace=%s", workspace_id)

    # 新连接推送 config_changed，让前端刷新模型列表等配置状态
    try:
        await websocket.send_json(make_event("config_changed", {"reason": "connect"}))
    except Exception:
        pass

    async def broadcast(data: dict) -> None:
        await workspace_connection_manager.broadcast(workspace_id, data)

    # 确保 SessionManager 能从 Agent 线程广播事件
    if sm and not sm._broadcast_fn:
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()

        async def _sm_broadcast(ws_id: str, data: dict) -> None:
            await workspace_connection_manager.broadcast(ws_id, data)

        sm.set_broadcast(loop, _sm_broadcast)

    context = RpcContext(
        workspace_id=workspace_id,
        broadcast=broadcast,
        managers={
            "workspace_manager": wm,
            "session_manager": sm,
            "terminal_manager": _get_terminal_manager(),
        },
        sender_ws=websocket,
    )

    try:
        while True:
            raw = await websocket.receive_json()
            response = await workspace_rpc.dispatch(raw, context)
            if response is not None:
                await websocket.send_json(response)
    except WebSocketDisconnect:
        logger.info("Workspace WS disconnected: workspace=%s", workspace_id)
    except Exception:
        logger.exception("Workspace WS error: workspace=%s", workspace_id)
    finally:
        workspace_connection_manager.disconnect(workspace_id, websocket)
