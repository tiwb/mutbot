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

from mutbot.runtime.menu import (
    AddSessionMenu,
    Menu,
    MenuItem,
    MenuRegistry,
    MenuResult,
    _get_attr_default,
    _item_to_dict,
    _menu_id,
    menu_registry,
)
from mutbot.web.rpc import RpcContext, RpcDispatcher


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _make_context(**kwargs) -> RpcContext:
    async def noop_broadcast(data: dict) -> None:
        pass
    return RpcContext(
        workspace_id=kwargs.get("workspace_id", "ws_test"),
        broadcast=kwargs.get("broadcast", noop_broadcast),
        managers=kwargs.get("managers", {}),
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
        assert _get_attr_default(Menu, "display_name") == ""
        assert _get_attr_default(Menu, "enabled") is True
        assert _get_attr_default(Menu, "visible") is True

    def test_reads_plain_value_override(self):
        """从子类读取无类型注解的纯值覆盖"""
        assert _get_attr_default(AddSessionMenu, "display_name") == "New Session"
        assert _get_attr_default(AddSessionMenu, "display_category") == "SessionPanel/Add"
        assert _get_attr_default(AddSessionMenu, "display_order") == "0new:0"

    def test_nonexistent_attr_returns_none(self):
        assert _get_attr_default(Menu, "nonexistent_attr") is None


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
        assert len(items) >= 3  # agent, terminal, document

        type_names = [it.data.get("session_type") for it in items]
        assert "agent" in type_names
        assert "terminal" in type_names
        assert "document" in type_names

    def test_dynamic_items_have_correct_format(self):
        ctx = _make_context()
        items = AddSessionMenu.dynamic_items(ctx)
        for item in items:
            assert isinstance(item, MenuItem)
            assert item.id.startswith("add_session:")
            assert item.name  # 非空
            assert item.order.startswith("0new:")

    def test_execute_creates_agent_session(self):
        """execute 应该通过 session_manager 创建 agent session"""

        class FakeSessionManager:
            def __init__(self):
                self.created = []

            def create(self, workspace_id, session_type, config=None):
                class FakeSession:
                    id = "fake_id"
                    title = "Agent 1"
                self.created.append((workspace_id, session_type))
                return FakeSession()

        fake_sm = FakeSessionManager()
        ctx = _make_context(
            workspace_id="ws_1",
            managers={"session_manager": fake_sm},
        )

        menu = AddSessionMenu()
        result = menu.execute({"session_type": "agent"}, ctx)

        assert isinstance(result, MenuResult)
        assert result.action == "session_created"
        assert result.data["session_id"] == "fake_id"
        assert result.data["session_type"] == "agent"
        assert result.data["title"] == "Agent 1"
        assert fake_sm.created == [("ws_1", "agent")]

    def test_execute_creates_terminal_session(self):
        """terminal session 创建时应同时创建 PTY"""

        class FakeTerm:
            id = "term_123"

        class FakeTerminalManager:
            def create(self, workspace_id, rows, cols, cwd=""):
                return FakeTerm()

        class FakeWorkspace:
            project_path = "/test"
            sessions = []

        class FakeWorkspaceManager:
            def get(self, wid):
                return FakeWorkspace()
            def update(self, ws):
                pass

        class FakeSessionManager:
            def create(self, workspace_id, session_type, config=None):
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
                "terminal_manager": FakeTerminalManager(),
                "workspace_manager": FakeWorkspaceManager(),
            },
        )

        menu = AddSessionMenu()
        result = menu.execute({"session_type": "terminal"}, ctx)

        assert result.action == "session_created"
        assert result.data["session_type"] == "terminal"
        assert fake_sm.last_config["terminal_id"] == "term_123"

    def test_execute_terminal_without_terminal_manager(self):
        """缺少 terminal_manager 时返回 error"""

        class FakeSessionManager:
            def create(self, workspace_id, session_type, config=None):
                class FakeSession:
                    id = "s1"
                    title = "T1"
                return FakeSession()

        ctx = _make_context(managers={"session_manager": FakeSessionManager()})
        menu = AddSessionMenu()
        result = menu.execute({"session_type": "terminal"}, ctx)
        assert result.action == "error"

    def test_execute_without_session_manager(self):
        ctx = _make_context(managers={})
        menu = AddSessionMenu()
        result = menu.execute({"session_type": "agent"}, ctx)
        assert result.action == "error"

    def test_execute_default_type_is_agent(self):
        class FakeSessionManager:
            def create(self, workspace_id, session_type, config=None):
                class FakeSession:
                    id = "s1"
                    title = "Agent 1"
                self.last_type = session_type
                return FakeSession()

        fake_sm = FakeSessionManager()
        ctx = _make_context(managers={"session_manager": fake_sm})
        menu = AddSessionMenu()
        menu.execute({}, ctx)  # 不传 session_type
        assert fake_sm.last_type == "agent"


# ---------------------------------------------------------------------------
# Menu RPC handlers（集成测试）
# ---------------------------------------------------------------------------

class TestMenuRpcHandlers:

    @pytest.mark.asyncio
    async def test_menu_query_handler(self):
        from mutbot.web.routes import workspace_rpc
        ctx = _make_context()

        msg = {
            "type": "rpc",
            "id": "r1",
            "method": "menu.query",
            "params": {"category": "SessionPanel/Add"},
        }
        resp = await workspace_rpc.dispatch(msg, ctx)

        assert resp["type"] == "rpc_result"
        assert resp["id"] == "r1"
        items = resp["result"]
        assert isinstance(items, list)
        assert len(items) >= 3

    @pytest.mark.asyncio
    async def test_menu_query_empty_category(self):
        from mutbot.web.routes import workspace_rpc
        ctx = _make_context()

        msg = {
            "type": "rpc",
            "id": "r2",
            "method": "menu.query",
            "params": {"category": "NonExistent"},
        }
        resp = await workspace_rpc.dispatch(msg, ctx)

        assert resp["type"] == "rpc_result"
        assert resp["result"] == []

    @pytest.mark.asyncio
    async def test_menu_execute_handler(self):
        from mutbot.web.routes import workspace_rpc

        class FakeSessionManager:
            def create(self, workspace_id, session_type, config=None):
                class FakeSession:
                    id = "new_session_id"
                    title = "Agent 1"
                return FakeSession()

        ctx = _make_context(
            workspace_id="ws_1",
            managers={"session_manager": FakeSessionManager()},
        )

        menu_id = _menu_id(AddSessionMenu)
        msg = {
            "type": "rpc",
            "id": "r3",
            "method": "menu.execute",
            "params": {
                "menu_id": menu_id,
                "params": {"session_type": "agent"},
            },
        }
        resp = await workspace_rpc.dispatch(msg, ctx)

        assert resp["type"] == "rpc_result"
        result = resp["result"]
        assert result["action"] == "session_created"
        assert result["data"]["session_id"] == "new_session_id"

    @pytest.mark.asyncio
    async def test_menu_execute_missing_menu_id(self):
        from mutbot.web.routes import workspace_rpc
        ctx = _make_context()

        msg = {
            "type": "rpc",
            "id": "r4",
            "method": "menu.execute",
            "params": {},
        }
        resp = await workspace_rpc.dispatch(msg, ctx)

        assert resp["type"] == "rpc_result"
        assert "error" in resp["result"]

    @pytest.mark.asyncio
    async def test_menu_execute_unknown_menu(self):
        from mutbot.web.routes import workspace_rpc
        ctx = _make_context()

        msg = {
            "type": "rpc",
            "id": "r5",
            "method": "menu.execute",
            "params": {"menu_id": "no.such.Menu"},
        }
        resp = await workspace_rpc.dispatch(msg, ctx)

        assert resp["type"] == "rpc_result"
        assert "error" in resp["result"]

    @pytest.mark.asyncio
    async def test_menu_methods_registered(self):
        from mutbot.web.routes import workspace_rpc
        assert "menu.query" in workspace_rpc.methods
        assert "menu.execute" in workspace_rpc.methods
