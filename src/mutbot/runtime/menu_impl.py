"""Menu 实现细节 — MenuRegistry、辅助函数和全局注册表实例。

Menu Declaration 基类和数据结构已迁移到 mutbot.menu（公开 API），
内置菜单子类已迁移到 mutbot.builtins.menus。
本模块保留 MenuRegistry 注册表实现和辅助工具。
"""

from __future__ import annotations

import logging
from typing import Any

import mutobj

from mutbot.menu import Menu, MenuItem, MenuResult
from mutbot.web.rpc import RpcContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 辅助函数
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
    if item.shortcut:
        d["shortcut"] = item.shortcut
    if item.client_action:
        d["client_action"] = item.client_action
    if item.data:
        d["data"] = item.data
    return d


# ---------------------------------------------------------------------------
# MenuRegistry — 按 category 索引的菜单注册表
# ---------------------------------------------------------------------------

class MenuRegistry:
    """按 display_category 索引的菜单注册表。

    使用 mutobj.get_registry_generation() 做变更检测缓存。
    """

    def __init__(self) -> None:
        self._cached_generation: int = -1
        self._cached_menus: list[type[Menu]] = []
        # 动态菜单项 ID → 生成该项的父 Menu 子类映射
        self._dynamic_item_owners: dict[str, type[Menu]] = {}

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

        # 从 RPC params 中提取上下文（前端传入的额外信息）
        menu_context: dict = {}
        if hasattr(context, "managers"):
            menu_context = getattr(context, "_menu_context", {})

        for menu_cls in menus:
            # 可见性判断
            visible = _get_attr_default(menu_cls, "visible")
            check_vis = menu_cls.check_visible(menu_context)
            if check_vis is not None:
                visible = check_vis
            if visible is not None and not visible:
                continue

            # 尝试动态展开
            dynamic = menu_cls.dynamic_items(context)
            if dynamic is not None:
                for item in dynamic:
                    self._dynamic_item_owners[item.id] = menu_cls
                items.extend(dynamic)
            else:
                # 静态菜单项
                enabled_val = _get_attr_default(menu_cls, "enabled")
                check_en = menu_cls.check_enabled(menu_context)
                if check_en is not None:
                    enabled_val = check_en

                shortcut = _get_attr_default(menu_cls, "display_shortcut") or ""
                client_act = _get_attr_default(menu_cls, "client_action") or ""

                items.append(MenuItem(
                    id=_menu_id(menu_cls),
                    name=_get_attr_default(menu_cls, "display_name") or menu_cls.__name__,
                    icon=_get_attr_default(menu_cls, "display_icon") or "",
                    order=_get_attr_default(menu_cls, "display_order") or "_",
                    enabled=enabled_val if enabled_val is not None else True,
                    visible=True,
                    shortcut=shortcut,
                    client_action=client_act,
                ))

        items.sort(key=lambda it: it.order)
        return [_item_to_dict(it) for it in items]

    def find_menu_class(self, menu_id: str) -> type[Menu] | None:
        """根据 menu_id 查找 Menu 子类。

        优先按类 ID 直接查找（静态菜单），
        其次从 dynamic_items 的 ID 映射中查找父类（动态菜单）。
        """
        self._refresh()
        for cls in self._cached_menus:
            if _menu_id(cls) == menu_id:
                return cls
        # 动态菜单项：查找生成该项的父 Menu 子类
        return self._dynamic_item_owners.get(menu_id)


# 全局注册表实例
menu_registry = MenuRegistry()
