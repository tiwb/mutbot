"""测试 Menu Declaration 体系（Phase 4）

涵盖：
- Menu Declaration 定义和属性
- MenuRegistry 发现、按 category 查询、排序
- dynamic_items 展开
- 静态菜单项生成
- _get_attr_default 函数（含纯值覆盖）
- AddSessionMenu 动态菜单
- menu.query / menu.execute RPC handler
"""

from __future__ import annotations

import asyncio

import pytest

import mutobj


from mutbot.menu import Menu, MenuItem, MenuResult
from mutbot.runtime.menu_impl import (
    MenuRegistry,
    menu_registry,
    _item_to_dict,
    _menu_id,
)
from mutbot.builtins.menus import AddSessionMenu
from mutbot.web.rpc import RpcContext, RpcDispatcher


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _make_context(**kwargs) -> RpcContext:
    async def noop_broadcast(data: dict) -> None:
        pass
    managers = kwargs.get("managers", {})
    return RpcContext(
        workspace_id=kwargs.get("workspace_id", "ws_test"),
        broadcast=kwargs.get("broadcast", noop_broadcast),
        session_manager=managers.get("session_manager"),
        workspace_manager=managers.get("workspace_manager"),
        terminal_manager=managers.get("terminal_manager"),
    )


# ---------------------------------------------------------------------------
# Menu Declaration 基础
# ---------------------------------------------------------------------------

class TestMenuDeclaration:

    def test_menu_is_declaration(self):
        assert issubclass(Menu, mutobj.Declaration)

    def test_menu_default_attributes(self):
        m = Menu()
        assert m.display_name == ""
        assert m.display_icon == ""
        assert m.display_order == "_"
        assert m.display_category == ""
        assert m.enabled is True
        assert m.visible is True

    def test_menu_subclass_registered(self):
        """AddSessionMenu 应该被 mutobj 自动注册"""
        all_menus = mutobj.discover_subclasses(Menu)
        class_names = [c.__name__ for c in all_menus]
        assert "AddSessionMenu" in class_names

    def test_dynamic_items_default_returns_none(self):
        ctx = _make_context()
        assert Menu.dynamic_items(ctx) is None


# ---------------------------------------------------------------------------
# _get_attr_default
# ---------------------------------------------------------------------------

class TestGetAttrDefault:

    def test_reads_descriptor_default(self):
        """从 Menu 基类读取 AttributeDescriptor 的默认值"""
        assert Menu.display_name.make_default() == ""
        assert Menu.enabled.make_default() is True
        assert Menu.visible.make_default() is True

    def test_reads_plain_value_override(self):
        """从子类读取无类型注解的纯值覆盖"""
        assert AddSessionMenu.display_name.make_default() == "New Session"
        assert AddSessionMenu.display_category.make_default() == "SessionPanel/Add"
        assert AddSessionMenu.display_order.make_default() == "0new:0"

# ---------------------------------------------------------------------------
# MenuItem / MenuResult 数据结构
# ---------------------------------------------------------------------------

class TestDataStructures:

    def test_menu_item_defaults(self):
        item = MenuItem(id="test", name="Test")
        assert item.icon == ""
        assert item.order == "_"
        assert item.enabled is True
        assert item.visible is True
        assert item.data == {}

    def test_menu_result_defaults(self):
        result = MenuResult()
        assert result.action == ""
        assert result.data == {}

    def test_item_to_dict(self):
        item = MenuItem(id="m1", name="Menu 1", icon="star", order="0:0")
        d = _item_to_dict(item)
        assert d == {
            "id": "m1",
            "name": "Menu 1",
            "icon": "star",
            "order": "0:0",
            "enabled": True,
            "visible": True,
        }

    def test_item_to_dict_with_data(self):
        item = MenuItem(id="m1", name="M", data={"key": "val"})
        d = _item_to_dict(item)
        assert d["data"] == {"key": "val"}

    def test_item_to_dict_without_data(self):
        item = MenuItem(id="m1", name="M")
        d = _item_to_dict(item)
        assert "data" not in d


# ---------------------------------------------------------------------------
# _menu_id
# ---------------------------------------------------------------------------

class TestMenuId:

    def test_menu_id_format(self):
        mid = _menu_id(AddSessionMenu)
        assert "AddSessionMenu" in mid
        assert "." in mid  # module.qualname 格式


# ---------------------------------------------------------------------------
# MenuRegistry
# ---------------------------------------------------------------------------

class TestMenuRegistry:

    def test_get_all(self):
        """全局 registry 能发现 AddSessionMenu"""
        all_menus = menu_registry.get_all()
        assert any(c.__name__ == "AddSessionMenu" for c in all_menus)

    def test_get_by_category(self):
        menus = menu_registry.get_by_category("SessionPanel/Add")
        assert len(menus) >= 1
        assert any(c.__name__ == "AddSessionMenu" for c in menus)

    def test_get_by_category_empty(self):
        menus = menu_registry.get_by_category("NonExistent/Category")
        assert menus == []

    def test_find_menu_class(self):
        mid = _menu_id(AddSessionMenu)
        found = menu_registry.find_menu_class(mid)
        assert found is AddSessionMenu

    def test_find_menu_class_not_found(self):
        assert menu_registry.find_menu_class("no.such.menu") is None

    def test_query_returns_dicts(self):
        ctx = _make_context()
        result = menu_registry.query("SessionPanel/Add", ctx)
        assert isinstance(result, list)
        assert len(result) >= 1
        # 每项应该是 dict
        for item in result:
            assert isinstance(item, dict)
            assert "id" in item
            assert "name" in item

    def test_query_empty_category(self):
        ctx = _make_context()
        result = menu_registry.query("NonExistent", ctx)
        assert result == []

    def test_query_items_sorted_by_order(self):
        ctx = _make_context()
        result = menu_registry.query("SessionPanel/Add", ctx)
        orders = [item["order"] for item in result]
        assert orders == sorted(orders)


# ---------------------------------------------------------------------------
# AddSessionMenu
# ---------------------------------------------------------------------------

class TestAddSessionMenu:

    def test_dynamic_items_returns_session_types(self):
        ctx = _make_context()
        items = AddSessionMenu.dynamic_items(ctx)
        assert isinstance(items, list)
        assert len(items) >= 1  # terminal

        type_names = [it.data.get("session_type") for it in items]
        assert "mutbot.session.TerminalSession" in type_names

    def test_dynamic_items_have_correct_format(self):
        ctx = _make_context()
        items = AddSessionMenu.dynamic_items(ctx)
        for item in items:
            assert isinstance(item, MenuItem)
            assert item.id.startswith("add_session:")
            assert item.name  # 非空
            assert item.order.startswith("0new:")

    @pytest.mark.asyncio
    async def test_execute_creates_terminal_session(self):
        """terminal session 创建时 cwd 写入 config，on_create 中创建 PTY"""

        class FakeWorkspace:
            sessions = []

        class FakeWorkspaceManager:
            def get(self, wid):
                return FakeWorkspace()
            def update(self, ws):
                pass

        class FakeSessionManager:
            async def create(self, workspace_id, session_type, config=None):
                self.last_config = config
                class FakeSession:
                    id = "s_term"
                    title = "Terminal 1"
                return FakeSession()

        fake_sm = FakeSessionManager()
        ctx = _make_context(
            workspace_id="ws_1",
            managers={
                "session_manager": fake_sm,
                "workspace_manager": FakeWorkspaceManager(),
            },
        )

        menu = AddSessionMenu()
        result = await menu.execute({"session_type": "mutbot.session.TerminalSession"}, ctx)

        assert result.action == "session_created"
        assert result.data["session_type"] == "mutbot.session.TerminalSession"
        from mutbot.runtime import storage
        assert fake_sm.last_config["cwd"] == storage.STARTUP_CWD

    @pytest.mark.asyncio
    async def test_execute_terminal_without_workspace_manager(self):
        """缺少 workspace_manager 时 cwd 不设置，仍能创建 session"""

        class FakeSessionManager:
            async def create(self, workspace_id, session_type, config=None):
                self.last_config = config
                class FakeSession:
                    id = "s1"
                    title = "T1"
                return FakeSession()

        fake_sm = FakeSessionManager()
        ctx = _make_context(managers={"session_manager": fake_sm})
        menu = AddSessionMenu()
        result = await menu.execute({"session_type": "mutbot.session.TerminalSession"}, ctx)
        assert result.action == "session_created"

    @pytest.mark.asyncio
    async def test_execute_without_session_manager(self):
        ctx = _make_context(managers={})
        menu = AddSessionMenu()
        result = await menu.execute({"session_type": "mutbot.session.AgentSession"}, ctx)
        assert result.action == "error"

    @pytest.mark.asyncio
    async def test_execute_no_type_returns_error(self):
        class FakeSessionManager:
            async def create(self, workspace_id, session_type, config=None):
                class FakeSession:
                    id = "s1"
                    title = "Guide 1"
                return FakeSession()

        fake_sm = FakeSessionManager()
        ctx = _make_context(managers={"session_manager": fake_sm})
        menu = AddSessionMenu()
        result = await menu.execute({}, ctx)  # 不传 session_type
        assert result.action == "error"


# ---------------------------------------------------------------------------
# Menu RPC handlers（集成测试）
# ---------------------------------------------------------------------------

class TestMenuRpcHandlers:

    @classmethod
    def setup_class(cls):
        import mutbot.web.rpc_session  # noqa: F401 — 触发 Declaration 注册
        import mutbot.web.rpc_workspace  # noqa: F401
        from mutbot.web.rpc import RpcDispatcher, WorkspaceRpc, SessionRpc
        cls._dispatcher = RpcDispatcher.from_declaration(WorkspaceRpc, SessionRpc)

    @pytest.mark.asyncio
    async def test_menu_query_handler(self):
        ctx = _make_context()

        msg = {
            "type": "rpc",
            "id": "r1",
            "method": "menu.query",
            "params": {"category": "SessionPanel/Add"},
        }
        resp = await self._dispatcher.dispatch(msg, ctx)

        assert resp["type"] == "rpc_result"
        assert resp["id"] == "r1"
        items = resp["result"]
        assert isinstance(items, list)
        assert len(items) >= 1

    @pytest.mark.asyncio
    async def test_menu_query_empty_category(self):
        ctx = _make_context()

        msg = {
            "type": "rpc",
            "id": "r2",
            "method": "menu.query",
            "params": {"category": "NonExistent"},
        }
        resp = await self._dispatcher.dispatch(msg, ctx)

        assert resp["type"] == "rpc_result"
        assert resp["result"] == []

    @pytest.mark.asyncio
    async def test_menu_execute_missing_menu_id(self):
        ctx = _make_context()

        msg = {
            "type": "rpc",
            "id": "r4",
            "method": "menu.execute",
            "params": {},
        }
        resp = await self._dispatcher.dispatch(msg, ctx)

        assert resp["type"] == "rpc_result"
        assert "error" in resp["result"]

    @pytest.mark.asyncio
    async def test_menu_execute_unknown_menu(self):
        ctx = _make_context()

        msg = {
            "type": "rpc",
            "id": "r5",
            "method": "menu.execute",
            "params": {"menu_id": "no.such.Menu"},
        }
        resp = await self._dispatcher.dispatch(msg, ctx)

        assert resp["type"] == "rpc_result"
        assert "error" in resp["result"]

    @pytest.mark.asyncio
    async def test_menu_methods_registered(self):
        assert "menu.query" in self._dispatcher.methods
        assert "menu.execute" in self._dispatcher.methods


# ---------------------------------------------------------------------------
# 新属性测试 (Phase 7)
# ---------------------------------------------------------------------------

class TestMenuNewAttributes:

    def test_menu_default_new_attributes(self):
        """Menu 基类的新属性默认值"""
        assert Menu.display_shortcut.make_default() == ""
        assert Menu.client_action.make_default() == ""

    def test_check_enabled_default_returns_none(self):
        assert Menu.check_enabled({}) is None

    def test_check_visible_default_returns_none(self):
        assert Menu.check_visible({}) is None


# ---------------------------------------------------------------------------
# Tab/Context 菜单测试
# ---------------------------------------------------------------------------

class TestTabContextMenus:

    def test_rename_session_menu_attributes(self):
        from mutbot.builtins.menus import RenameSessionMenu
        assert RenameSessionMenu.display_name.make_default() == "Rename"
        assert RenameSessionMenu.display_category.make_default() == "Tab/Context"
        assert RenameSessionMenu.display_shortcut.make_default() == "F2"
        assert RenameSessionMenu.client_action.make_default() == "start_rename"

    def test_close_tab_menu_attributes(self):
        from mutbot.builtins.menus import CloseTabMenu
        assert CloseTabMenu.display_name.make_default() == "Close"
        assert CloseTabMenu.client_action.make_default() == "close_tab"

    def test_close_others_menu_attributes(self):
        from mutbot.builtins.menus import CloseOthersMenu
        assert CloseOthersMenu.display_name.make_default() == "Close Others"
        assert CloseOthersMenu.client_action.make_default() == "close_others"

    def test_tab_context_discovery(self):
        menus = menu_registry.get_by_category("Tab/Context")
        names = [c.__name__ for c in menus]
        assert "RenameSessionMenu" in names
        assert "CloseTabMenu" in names
        assert "CloseOthersMenu" in names

    def test_tab_context_query(self):
        ctx = _make_context()
        result = menu_registry.query("Tab/Context", ctx)
        assert len(result) >= 4
        names = [it["name"] for it in result]
        assert "Rename" in names
        assert "Close" in names

    def test_tab_context_query_with_shortcut(self):
        ctx = _make_context()
        result = menu_registry.query("Tab/Context", ctx)
        rename_item = next(it for it in result if it["name"] == "Rename")
        assert rename_item.get("shortcut") == "F2"

    def test_tab_context_query_with_client_action(self):
        ctx = _make_context()
        result = menu_registry.query("Tab/Context", ctx)
        close_item = next(it for it in result if it["name"] == "Close")
        assert close_item.get("client_action") == "close_tab"


# ---------------------------------------------------------------------------
# SessionList/Context 菜单测试
# ---------------------------------------------------------------------------

class TestSessionListContextMenus:

    def test_rename_session_list_menu_attributes(self):
        from mutbot.builtins.menus import RenameSessionListMenu
        assert RenameSessionListMenu.display_name.make_default() == "Rename"
        assert RenameSessionListMenu.display_category.make_default() == "SessionList/Context"
        assert RenameSessionListMenu.client_action.make_default() == "start_rename"

    def test_delete_session_menu_attributes(self):
        from mutbot.builtins.menus import DeleteSessionMenu
        assert DeleteSessionMenu.display_name.make_default() == "Delete"
        assert DeleteSessionMenu.display_category.make_default() == "SessionList/Context"

    def test_session_list_context_discovery(self):
        menus = menu_registry.get_by_category("SessionList/Context")
        names = [c.__name__ for c in menus]
        assert "RenameSessionListMenu" in names
        assert "DeleteSessionMenu" in names

    def test_session_list_context_query(self):
        ctx = _make_context()
        result = menu_registry.query("SessionList/Context", ctx)
        assert len(result) >= 3
        names = [it["name"] for it in result]
        assert "Rename" in names
        assert "Delete" in names

    def test_session_list_context_sorted_by_order(self):
        ctx = _make_context()
        result = menu_registry.query("SessionList/Context", ctx)
        orders = [it["order"] for it in result]
        assert orders == sorted(orders)


# ---------------------------------------------------------------------------
# MenuItem 新字段序列化测试
# ---------------------------------------------------------------------------

class TestMenuItemNewFields:

    def test_item_with_shortcut(self):
        item = MenuItem(id="t1", name="Test", shortcut="Ctrl+S")
        d = _item_to_dict(item)
        assert d["shortcut"] == "Ctrl+S"

    def test_item_without_shortcut(self):
        item = MenuItem(id="t1", name="Test")
        d = _item_to_dict(item)
        assert "shortcut" not in d

    def test_item_with_client_action(self):
        item = MenuItem(id="t1", name="Test", client_action="do_something")
        d = _item_to_dict(item)
        assert d["client_action"] == "do_something"

    def test_item_without_client_action(self):
        item = MenuItem(id="t1", name="Test")
        d = _item_to_dict(item)
        assert "client_action" not in d

    def test_item_with_both_new_fields(self):
        item = MenuItem(id="t1", name="Test", shortcut="F2", client_action="rename")
        d = _item_to_dict(item)
        assert d["shortcut"] == "F2"
        assert d["client_action"] == "rename"
