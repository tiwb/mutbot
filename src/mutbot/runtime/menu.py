"""Menu Declaration 体系 — 菜单定义、注册表和数据结构。

Menu 基于 mutobj.Declaration，支持通过继承定义菜单项。
MenuRegistry 按 display_category 索引，配合 mutobj 子类发现 API
实现运行时动态菜单。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import mutobj

from mutbot.web.rpc import RpcContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class MenuItem:
    """单个菜单项的数据表示（传递给前端渲染）"""

    id: str
    name: str
    icon: str = ""
    order: str = "_"
    enabled: bool = True
    visible: bool = True
    # 额外数据（前端可选使用）
    data: dict = field(default_factory=dict)


@dataclass
class MenuResult:
    """Menu.execute() 的返回值"""

    action: str = ""
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Menu Declaration
# ---------------------------------------------------------------------------

class Menu(mutobj.Declaration):
    """菜单项基类

    子类定义具体菜单，通过 display_category 指定在哪里显示，
    通过 display_order 控制排序和分组。

    display_order 格式: "group:index"
    - 字符串字典序排列
    - 同 group 归为一组，组间显示分隔线
    - 示例: "0new:0" < "0new:1" < "1manage:0"
    """

    # 显示属性
    display_name: str = ""
    display_icon: str = ""
    display_order: str = "_"
    display_category: str = ""

    # 行为属性
    enabled: bool = True
    visible: bool = True

    def execute(self, params: dict, context: RpcContext) -> MenuResult:
        """执行菜单动作，由子类实现"""
        ...

    @classmethod
    def dynamic_items(cls, context: RpcContext) -> list[MenuItem] | None:
        """运行时动态生成菜单项。

        返回 None 表示使用静态定义（默认行为）。
        返回 list[MenuItem] 则替代静态菜单项。
        """
        return None


# ---------------------------------------------------------------------------
# MenuRegistry — 按 category 索引的菜单注册表
# ---------------------------------------------------------------------------

def _get_attr_default(cls: type, attr: str) -> Any:
    """从 Declaration 子类的属性描述符中读取默认值。

    遍历 MRO，优先返回 AttributeDescriptor 的 default，
    也兼容子类中无类型注解的纯值覆盖（plain value override）。
    """
    for klass in cls.__mro__:
        desc = klass.__dict__.get(attr)
        if desc is None:
            continue
        # AttributeDescriptor 有 has_default 属性
        if hasattr(desc, "has_default") and desc.has_default:
            return desc.default
        # 子类中无类型注解的纯值覆盖（如 display_category = "SessionPanel/Add"）
        if not hasattr(desc, "has_default") and not callable(desc) and not isinstance(desc, property):
            return desc
    return None


class MenuRegistry:
    """按 display_category 索引的菜单注册表。

    使用 mutobj.get_registry_generation() 做变更检测缓存。
    """

    def __init__(self) -> None:
        self._cached_generation: int = -1
        self._cached_menus: list[type[Menu]] = []

    def _refresh(self) -> None:
        gen = mutobj.get_registry_generation()
        if gen != self._cached_generation:
            self._cached_generation = gen
            self._cached_menus = mutobj.discover_subclasses(Menu)

    def get_all(self) -> list[type[Menu]]:
        """返回所有已注册的 Menu 子类"""
        self._refresh()
        return list(self._cached_menus)

    def get_by_category(self, category: str) -> list[type[Menu]]:
        """返回指定 category 下的 Menu 子类，按 display_order 排序"""
        self._refresh()
        result = []
        for cls in self._cached_menus:
            cat = _get_attr_default(cls, "display_category")
            if cat == category:
                result.append(cls)
        result.sort(key=lambda c: _get_attr_default(c, "display_order") or "_")
        return result

    def query(self, category: str, context: RpcContext) -> list[dict]:
        """查询指定 category 的菜单项，返回可序列化的 dict 列表。

        处理逻辑：
        1. 扫描该 category 下的 Menu 子类
        2. 对支持 dynamic_items 的菜单，调用 dynamic_items() 展开
        3. 对静态菜单，生成单个 MenuItem
        4. 按 display_order 排序并返回
        """
        menus = self.get_by_category(category)
        items: list[MenuItem] = []

        for menu_cls in menus:
            visible = _get_attr_default(menu_cls, "visible")
            if visible is not None and not visible:
                continue

            # 尝试动态展开
            dynamic = menu_cls.dynamic_items(context)
            if dynamic is not None:
                items.extend(dynamic)
            else:
                # 静态菜单项
                enabled_val = _get_attr_default(menu_cls, "enabled")
                items.append(MenuItem(
                    id=_menu_id(menu_cls),
                    name=_get_attr_default(menu_cls, "display_name") or menu_cls.__name__,
                    icon=_get_attr_default(menu_cls, "display_icon") or "",
                    order=_get_attr_default(menu_cls, "display_order") or "_",
                    enabled=enabled_val if enabled_val is not None else True,
                    visible=True,
                ))

        items.sort(key=lambda it: it.order)
        return [_item_to_dict(it) for it in items]

    def find_menu_class(self, menu_id: str) -> type[Menu] | None:
        """根据 menu_id 查找 Menu 子类"""
        self._refresh()
        for cls in self._cached_menus:
            if _menu_id(cls) == menu_id:
                return cls
        return None


def _menu_id(cls: type[Menu]) -> str:
    """从 Menu 子类生成唯一 ID"""
    return f"{cls.__module__}.{cls.__qualname__}"


def _item_to_dict(item: MenuItem) -> dict:
    d: dict[str, Any] = {
        "id": item.id,
        "name": item.name,
        "icon": item.icon,
        "order": item.order,
        "enabled": item.enabled,
        "visible": item.visible,
    }
    if item.data:
        d["data"] = item.data
    return d


# 全局注册表实例
menu_registry = MenuRegistry()


# ---------------------------------------------------------------------------
# 内置菜单：添加 Session
# ---------------------------------------------------------------------------

# Session 类型 → (显示名, 图标标识) 映射
_SESSION_TYPE_LABELS: dict[str, tuple[str, str]] = {
    "agent": ("Agent Session", "agent"),
    "terminal": ("Terminal", "terminal"),
    "document": ("Document", "document"),
}


class AddSessionMenu(Menu):
    """动态菜单：根据已注册的 Session 子类生成 \"新建 Session\" 菜单项"""

    display_name = "New Session"
    display_category = "SessionPanel/Add"
    display_order = "0new:0"

    @classmethod
    def dynamic_items(cls, context: RpcContext) -> list[MenuItem]:
        """根据已注册的 Session 子类动态生成菜单项"""
        from mutbot.runtime.session import Session, _get_type_default

        items: list[MenuItem] = []
        idx = 0
        for session_cls in mutobj.discover_subclasses(Session):
            type_name = _get_type_default(session_cls)
            if not type_name:
                continue
            label, icon = _SESSION_TYPE_LABELS.get(
                type_name, (session_cls.__name__, "")
            )
            items.append(MenuItem(
                id=f"add_session:{type_name}",
                name=label,
                icon=icon,
                order=f"0new:{idx}",
                data={"session_type": type_name},
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

        session_type = params.get("session_type", "agent")
        sm = context.managers.get("session_manager")
        if sm is None:
            return MenuResult(action="error", data={"message": "session_manager not available"})

        config: dict = {}

        if session_type == "terminal":
            tm = context.managers.get("terminal_manager")
            wm = context.managers.get("workspace_manager")
            if tm is None:
                return MenuResult(action="error", data={"message": "terminal_manager not available"})
            ws = wm.get(context.workspace_id) if wm else None
            cwd = ws.project_path if ws else "."
            term = tm.create(context.workspace_id, 24, 80, cwd=cwd)
            config["terminal_id"] = term.id
        elif session_type == "document":
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
