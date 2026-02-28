"""Menu Declaration 基类 — 公开 API。

定义 Menu 基类和相关数据结构（MenuItem、MenuResult）。
Menu 基于 mutobj.Declaration，支持通过继承定义菜单项。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import mutobj

from mutbot.web.rpc import RpcContext


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
    # 快捷键显示文本（仅供前端渲染，不处理键盘事件）
    shortcut: str = ""
    # 前端直接处理的动作标识（非空时前端不走 menu.execute RPC）
    client_action: str = ""
    # 额外数据（前端可选使用）
    data: dict = field(default_factory=dict)
    # 非空时表示这是一个子菜单触发项，值为子菜单的 category
    submenu_category: str = ""


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
    display_shortcut: str = ""

    # 行为属性
    enabled: bool = True
    visible: bool = True
    client_action: str = ""
    # 非空时，此菜单项作为子菜单父项，子菜单内容为该 category
    display_submenu_category: str = ""

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

    @classmethod
    def check_enabled(cls, context: dict) -> bool | None:
        """根据上下文判断是否启用。返回 None 使用默认值。"""
        return None

    @classmethod
    def check_visible(cls, context: dict) -> bool | None:
        """根据上下文判断是否可见。返回 None 使用默认值。"""
        return None
