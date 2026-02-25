"""内置通用菜单 — 添加 Session、Tab 右键菜单、Session 列表右键菜单。"""

from __future__ import annotations

import mutobj

from mutbot.menu import Menu, MenuItem, MenuResult
from mutbot.web.rpc import RpcContext


# ---------------------------------------------------------------------------
# Session 类型显示信息：全限定名 → (显示名, 图标)
# ---------------------------------------------------------------------------

_SESSION_DISPLAY: dict[str, tuple[str, str]] = {
    "mutbot.builtins.guide.GuideSession": ("Guide", "guide"),
    "mutbot.builtins.researcher.ResearcherSession": ("Researcher", "researcher"),
    "mutbot.session.AgentSession": ("Agent Session", "agent"),
    "mutbot.session.TerminalSession": ("Terminal", "terminal"),
    "mutbot.session.DocumentSession": ("Document", "document"),
}


def _session_display(cls: type) -> tuple[str, str]:
    """获取 Session 子类的 (显示名, 图标)，未注册时从类名推导。"""
    qualified = f"{cls.__module__}.{cls.__qualname__}"
    if qualified in _SESSION_DISPLAY:
        return _SESSION_DISPLAY[qualified]
    # 回退：从类名推导 ("GuideSession" → "Guide Session")
    name = cls.__name__
    if name.endswith("Session"):
        name = name[:-7] + " Session"
    return (name, "")


# ---------------------------------------------------------------------------
# 内置菜单：添加 Session (SessionPanel/Add)
# ---------------------------------------------------------------------------

class AddSessionMenu(Menu):
    """动态菜单：根据已注册的 Session 子类生成 \"新建 Session\" 菜单项"""

    display_name = "New Session"
    display_category = "SessionPanel/Add"
    display_order = "0new:0"

    @classmethod
    def dynamic_items(cls, context: RpcContext) -> list[MenuItem]:
        """根据已注册的 Session 子类动态生成菜单项"""
        from mutbot.session import Session

        items: list[MenuItem] = []
        idx = 0
        for session_cls in mutobj.discover_subclasses(Session):
            qualified = f"{session_cls.__module__}.{session_cls.__qualname__}"
            label, icon = _session_display(session_cls)
            items.append(MenuItem(
                id=f"add_session:{qualified}",
                name=label,
                icon=icon,
                order=f"0new:{idx}",
                data={"session_type": qualified},
            ))
            idx += 1
        return items

    def execute(self, params: dict, context: RpcContext) -> MenuResult:
        """创建指定类型的 Session。

        处理特殊逻辑：
        - terminal: 创建 PTY 并将 terminal_id 写入 config
        - document: 生成默认文件路径写入 config
        """
        import time

        from mutbot.session import (
            TerminalSession, DocumentSession, get_session_class, DEFAULT_SESSION_TYPE,
        )

        session_type = params.get("session_type", DEFAULT_SESSION_TYPE)
        sm = context.managers.get("session_manager")
        if sm is None:
            return MenuResult(action="error", data={"message": "session_manager not available"})

        # 查找 Session 类以判断类型
        try:
            session_cls = get_session_class(session_type)
        except ValueError:
            return MenuResult(action="error", data={"message": f"unknown session type: {session_type}"})

        config: dict = {}

        if issubclass(session_cls, TerminalSession):
            tm = context.managers.get("terminal_manager")
            wm = context.managers.get("workspace_manager")
            if tm is None:
                return MenuResult(action="error", data={"message": "terminal_manager not available"})
            ws = wm.get(context.workspace_id) if wm else None
            cwd = ws.project_path if ws else "."
            term = tm.create(context.workspace_id, 24, 80, cwd=cwd)
            config["terminal_id"] = term.id
        elif issubclass(session_cls, DocumentSession):
            config["file_path"] = f"untitled-{int(time.time() * 1000)}.md"

        session = sm.create(
            workspace_id=context.workspace_id,
            session_type=session_type,
            config=config if config else None,
        )

        # 将 workspace 的 sessions 列表也更新
        if context.managers.get("workspace_manager"):
            wm = context.managers["workspace_manager"]
            ws = wm.get(context.workspace_id)
            if ws:
                ws.sessions.append(session.id)
                wm.update(ws)

        return MenuResult(
            action="session_created",
            data={
                "session_id": session.id,
                "session_type": session_type,
                "title": session.title,
            },
        )


# ---------------------------------------------------------------------------
# 内置菜单：Tab 右键菜单 (Tab/Context)
# ---------------------------------------------------------------------------

class RenameSessionMenu(Menu):
    """Tab 右键菜单 — 重命名"""
    display_name = "Rename"
    display_icon = "rename"
    display_category = "Tab/Context"
    display_order = "0basic:0"
    display_shortcut = "F2"
    client_action = "start_rename"


class CloseTabMenu(Menu):
    """Tab 右键菜单 — 关闭 Tab"""
    display_name = "Close"
    display_icon = "close"
    display_category = "Tab/Context"
    display_order = "0basic:1"
    client_action = "close_tab"


class CloseOthersMenu(Menu):
    """Tab 右键菜单 — 关闭其他 Tab"""
    display_name = "Close Others"
    display_category = "Tab/Context"
    display_order = "0basic:2"
    client_action = "close_others"


class EndSessionMenu(Menu):
    """Tab 右键菜单 — 结束 Session"""
    display_name = "End Session"
    display_icon = "stop"
    display_category = "Tab/Context"
    display_order = "1manage:0"

    @classmethod
    def check_enabled(cls, context: dict) -> bool | None:
        status = context.get("session_status")
        if status is not None:
            return status == "active"
        return None

    def execute(self, params: dict, context: RpcContext) -> MenuResult:
        sm = context.managers.get("session_manager")
        session_id = params.get("session_id", "")
        if not sm or not session_id:
            return MenuResult(action="error", data={"message": "missing session_manager or session_id"})
        # 实际的 async stop 由 handle_menu_execute 处理
        return MenuResult(action="session_ended", data={"session_id": session_id})


# ---------------------------------------------------------------------------
# 内置菜单：Session 列表右键菜单 (SessionList/Context)
# ---------------------------------------------------------------------------

class RenameSessionListMenu(Menu):
    """Session 列表右键菜单 — 重命名"""
    display_name = "Rename"
    display_category = "SessionList/Context"
    display_order = "0basic:0"
    client_action = "start_rename"


class EndSessionListMenu(Menu):
    """Session 列表右键菜单 — 结束 Session"""
    display_name = "End Session"
    display_category = "SessionList/Context"
    display_order = "1manage:0"

    @classmethod
    def check_enabled(cls, context: dict) -> bool | None:
        status = context.get("session_status")
        if status is not None:
            return status == "active"
        return None

    def execute(self, params: dict, context: RpcContext) -> MenuResult:
        sm = context.managers.get("session_manager")
        session_id = params.get("session_id", "")
        if not sm or not session_id:
            return MenuResult(action="error", data={"message": "missing session_manager or session_id"})
        # 实际的 async stop 由 handle_menu_execute 处理
        return MenuResult(action="session_ended", data={"session_id": session_id})


class DeleteSessionMenu(Menu):
    """Session 列表右键菜单 — 删除 Session"""
    display_name = "Delete"
    display_category = "SessionList/Context"
    display_order = "2danger:0"

    def execute(self, params: dict, context: RpcContext) -> MenuResult:
        sm = context.managers.get("session_manager")
        session_id = params.get("session_id", "")
        if not sm or not session_id:
            return MenuResult(action="error", data={"message": "missing session_manager or session_id"})
        # 实际的 async stop + delete 由 handle_menu_execute 处理
        return MenuResult(action="session_deleted", data={"session_id": session_id})
