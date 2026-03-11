"""App 级 RPC handler — workspace CRUD、filesystem、app menu。

注册到 app_rpc dispatcher（从 routes.py 导入）。
"""

from __future__ import annotations

from pathlib import Path

from mutbot.web.rpc import RpcContext
from mutbot.web.serializers import workspace_dict
from mutbot.runtime.menu_impl import menu_registry
from mutbot.menu import MenuResult

_registered = False


def register_app_rpc(app_rpc) -> None:
    """注册 App 级 RPC handler。"""
    global _registered
    if _registered:
        return
    _registered = True

    @app_rpc.method("workspace.list")
    async def handle_app_workspace_list(params: dict, ctx: RpcContext) -> list[dict]:
        """列出所有工作区。"""
        wm = ctx.workspace_manager
        if not wm:
            return []
        return [workspace_dict(ws) for ws in wm.list_all()]

    @app_rpc.method("workspace.create")
    async def handle_app_workspace_create(params: dict, ctx: RpcContext) -> dict:
        """创建工作区。"""
        wm = ctx.workspace_manager
        if not wm:
            return {"error": "workspace_manager not available"}

        project_path = params.get("project_path", "")
        if not project_path:
            return {"error": "missing project_path"}

        p = Path(project_path)
        if not p.is_absolute():
            return {"error": "project_path must be absolute"}

        create_dir = bool(params.get("create_dir", False))
        if create_dir:
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {"error": f"cannot create directory: {e}"}
        elif not p.is_dir():
            return {"error": "project_path does not exist or is not a directory"}

        name = params.get("name") or p.name
        ws = wm.create(name, str(p))

        # 无 LLM 配置时，创建默认 AgentSession 供配置向导使用
        _cfg = ctx.config
        if not _cfg or not _cfg.get("providers"):
            sm = ctx.session_manager
            if sm:
                agent_type = "mutbot.session.AgentSession"
                existing = sm.list_by_workspace(ws.id)
                agent_session = next(
                    (s for s in existing if s.type == agent_type),
                    None,
                )
                if agent_session is None:
                    agent_session = await sm.create(ws.id, session_type=agent_type)
                    ws.sessions.append(agent_session.id)
                    wm.update(ws)
                # 前端连接后自动打开 tab
                from mutbot.web.routes import queue_workspace_event
                queue_workspace_event(
                    ws.id, "open_session", {"session_id": agent_session.id},
                )

        return workspace_dict(ws)

    @app_rpc.method("filesystem.browse")
    async def handle_filesystem_browse(params: dict, ctx: RpcContext) -> dict:
        """列出目录内容（仅子目录）。"""
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
        """从注册表移除工作区（不删除数据文件）。"""
        wm = ctx.workspace_manager
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
        result = await menu_instance.execute(execute_params, ctx)

        if not isinstance(result, MenuResult):
            return result if isinstance(result, dict) else {}

        result_dict: dict = {"action": result.action, "data": result.data}

        # workspace_removed: 从注册表移除
        if result.action == "workspace_removed":
            wm = ctx.workspace_manager
            ws_id = result.data.get("workspace_id", "")
            if wm and ws_id:
                wm.remove(ws_id)

        return result_dict
