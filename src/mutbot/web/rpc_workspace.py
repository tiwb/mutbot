"""Workspace 级 RPC handler — workspace/terminal/file/config/menu。

Declaration 子类，自动发现注册到 WorkspaceRpc dispatcher。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mutbot.runtime import storage
from mutbot.web.rpc import WorkspaceRpc, RpcContext
from mutbot.web.serializers import (
    workspace_dict, session_dict, LANG_MAP,
)
from mutbot.runtime.menu_impl import menu_registry
from mutbot.menu import MenuResult


class MenuOps(WorkspaceRpc):
    """Workspace 级菜单操作。"""
    namespace = "menu"

    async def query(self, params: dict, ctx: RpcContext) -> list[dict]:
        """查询指定 category 的菜单项列表"""
        category = params.get("category", "")
        menu_context = params.get("context", {})
        ctx._menu_context = menu_context  # type: ignore[attr-defined]
        return menu_registry.query(category, ctx)

    async def execute(self, params: dict, ctx: RpcContext) -> dict:
        """执行指定菜单项。"""
        menu_id = params.get("menu_id", "")
        if not menu_id:
            return {"error": "missing menu_id"}

        menu_cls = menu_registry.find_menu_class(menu_id)
        if menu_cls is None:
            return {"error": f"menu not found: {menu_id}"}

        menu_instance = menu_cls()
        execute_params = params.get("params", {})
        result = menu_instance.execute(execute_params, ctx)
        # execute 可能是 async 方法，需要 await
        if hasattr(result, "__await__"):
            result = await result

        if not isinstance(result, MenuResult):
            return result if isinstance(result, dict) else {}

        result_dict: dict = {"action": result.action, "data": result.data}

        sm = ctx.session_manager
        session_id = result.data.get("session_id", "")

        if result.action == "session_created" and sm and session_id:
            session = sm.get(session_id)
            if session:
                await ctx.broadcast_event("session_created", session_dict(session))

        elif result.action == "session_deleted" and sm and session_id:
            from mutbot.web.routes import _close_channels_for_session
            _close_channels_for_session(session_id, "session_deleted")
            await sm.stop(session_id)
            sm.delete(session_id)
            wm = ctx.workspace_manager
            if wm:
                ws = wm.get(ctx.workspace_id)
                if ws and session_id in ws.sessions:
                    ws.sessions.remove(session_id)
                    wm.update(ws)
            await ctx.broadcast_event("session_deleted", {"session_id": session_id})

        elif result.action == "session_deleted_batch" and sm:
            from mutbot.web.routes import _close_channels_for_session
            batch_ids = result.data.get("session_ids", [])
            wm = ctx.workspace_manager
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


class WorkspaceDetailOps(WorkspaceRpc):
    """Workspace 详情操作。"""
    namespace = "workspace"

    async def get(self, params: dict, ctx: RpcContext) -> dict:
        """获取 workspace 详情"""
        wm = ctx.workspace_manager
        if not wm:
            return {"error": "workspace_manager not available"}
        workspace_id = params.get("workspace_id", ctx.workspace_id)
        ws = wm.get(workspace_id)
        if ws is None:
            return {"error": "workspace not found"}
        return workspace_dict(ws)

    async def update(self, params: dict, ctx: RpcContext) -> dict:
        """更新 workspace 字段（如 layout）"""
        wm = ctx.workspace_manager
        if not wm:
            return {"error": "workspace_manager not available"}
        workspace_id = params.get("workspace_id", ctx.workspace_id)
        ws = wm.get(workspace_id)
        if ws is None:
            return {"error": "workspace not found"}
        if "layout" in params:
            ws.layout = params["layout"]
        wm.update(ws)
        return workspace_dict(ws)

    async def reorder_sessions(self, params: dict, ctx: RpcContext) -> dict:
        """更新 workspace 中的 session 排列顺序。"""
        wm = ctx.workspace_manager
        if not wm:
            return {"error": "workspace_manager not available"}
        ws = wm.get(ctx.workspace_id)
        if ws is None:
            return {"error": "workspace not found"}
        new_order = params.get("session_ids", [])
        if set(new_order) != set(ws.sessions):
            return {"error": "session_ids mismatch"}
        ws.sessions = new_order
        wm.update(ws)
        return {"status": "ok"}


class TerminalOps(WorkspaceRpc):
    """终端操作。"""
    namespace = "terminal"

    async def create(self, params: dict, ctx: RpcContext) -> dict:
        """创建终端"""
        wm = ctx.workspace_manager
        tm = ctx.terminal_manager
        if not wm or not tm:
            return {"error": "managers not available"}
        ws = wm.get(ctx.workspace_id)
        if ws is None:
            return {"error": "workspace not found"}
        rows = params.get("rows", 24)
        cols = params.get("cols", 80)
        term_id = await tm.create(rows, cols, cwd=params.get("cwd") or storage.STARTUP_CWD)
        data = {"term_id": term_id, "rows": rows, "cols": cols}
        await ctx.broadcast_event("terminal_created", data)
        return data

    async def list(self, params: dict, ctx: RpcContext) -> list[dict]:
        """列出 workspace 下的所有终端"""
        tm = ctx.terminal_manager
        if not tm:
            return []
        return [{"term_id": t["term_id"], "alive": t["alive"]}
                for t in await tm.list_terminals()]

    async def delete(self, params: dict, ctx: RpcContext) -> dict:
        """删除终端"""
        tm = ctx.terminal_manager
        if not tm:
            return {"error": "terminal_manager not available"}
        term_id = params.get("term_id", "")
        if not tm.has(term_id):
            return {"error": "terminal not found"}
        await tm.notify_exit(term_id)
        tm.kill(term_id)
        await ctx.broadcast_event("terminal_deleted", {"term_id": term_id})
        return {"status": "killed"}


class FileOps(WorkspaceRpc):
    """文件操作。"""
    namespace = "file"

    async def read(self, params: dict, ctx: RpcContext) -> dict:
        """读取文件内容（接受绝对路径）"""
        file_path = params.get("path", "")
        if not file_path:
            return {"error": "missing path"}

        target = Path(file_path).resolve()
        if not target.is_file():
            return {"error": "file not found"}

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"error": str(exc)}

        ext = target.suffix.lower()
        language = LANG_MAP.get(ext, "plaintext")
        return {"path": str(target), "content": content, "language": language}


class ConfigOps(WorkspaceRpc):
    """配置操作。"""
    namespace = "config"

    async def models(self, params: dict, ctx: RpcContext) -> dict:
        """返回所有已配置的模型列表"""
        from mutagent.provider import LLMProvider

        sm = ctx.session_manager
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


class DebugRpc(WorkspaceRpc):
    """调试工具 RPC — 接收浏览器 JS 执行结果。"""
    namespace = "debug"

    async def eval_result(self, params: dict, ctx: RpcContext) -> dict:
        """接收前端 eval_js 的执行结果，resolve 对应的 Future。"""
        from mutbot.web.mcp import _eval_js_pending

        eval_id = params.get("id", "")
        future = _eval_js_pending.get(eval_id)
        if future and not future.done():
            future.set_result({
                "result": params.get("result"),
                "error": params.get("error"),
            })
            return {"ok": True}
        return {"error": f"no pending eval: {eval_id}"}


class ClientOps(WorkspaceRpc):
    """客户端元信息操作。"""
    namespace = "client"

    async def setInfo(self, params: dict, ctx: RpcContext) -> dict:
        """前端推送客户端元信息（origin 等），更新 Client 对象。"""
        client = ctx.get_sender_client()
        if client is None:
            return {"error": "client not found"}
        origin = params.get("origin")
        if origin and isinstance(origin, str):
            client.origin = origin
        return {"ok": True}
