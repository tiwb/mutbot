"""App 级 RPC handler — workspace CRUD、filesystem、app menu。

Declaration 子类，自动发现注册到 AppRpc dispatcher。
"""

from __future__ import annotations

from mutbot.web.rpc import AppRpc, RpcContext
from mutbot.web.serializers import workspace_dict
from mutbot.runtime.menu_impl import menu_registry
from mutbot.menu import MenuResult


class WorkspaceOps(AppRpc):
    """workspace CRUD 操作。"""
    namespace = "workspace"

    async def list(self, params: dict, ctx: RpcContext) -> list[dict]:
        """列出所有工作区。"""
        wm = ctx.workspace_manager
        if not wm:
            return []
        return [workspace_dict(ws) for ws in wm.list_all()]

    async def create(self, params: dict, ctx: RpcContext) -> dict:
        """创建工作区（只需名称）。"""
        wm = ctx.workspace_manager
        if not wm:
            return {"error": "workspace_manager not available"}

        name = params.get("name", "")
        if not name:
            return {"error": "missing name"}

        ws = wm.create(name)

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

    async def remove(self, params: dict, ctx: RpcContext) -> dict:
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


class AppMenuOps(AppRpc):
    """App 级菜单操作。"""
    namespace = "menu"

    async def query(self, params: dict, ctx: RpcContext) -> list[dict]:
        """App 级菜单查询。"""
        category = params.get("category", "")
        menu_context = params.get("context", {})
        ctx._menu_context = menu_context  # type: ignore[attr-defined]
        return menu_registry.query(category, ctx)

    async def execute(self, params: dict, ctx: RpcContext) -> dict:
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
