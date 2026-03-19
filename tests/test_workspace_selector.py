"""测试工作区选择器后端功能

涵盖：
- sanitize_workspace_name 各种输入
- 工作区名称唯一性（重复名称自动加后缀）
- get_by_name 查找
- 注册表（registry）读写与 WorkspaceManager 注册表集成
- App RPC handlers (workspace.list / workspace.create / workspace.remove)
"""

from __future__ import annotations

from unittest import mock

import pytest

from mutbot.runtime.workspace import WorkspaceManager, sanitize_workspace_name
from mutbot.runtime import storage
from mutbot.web.rpc import RpcContext
from mutbot.web.rpc_app import WorkspaceOps


# ---------------------------------------------------------------------------
# sanitize_workspace_name 测试
# ---------------------------------------------------------------------------

class TestSanitizeWorkspaceName:
    def test_basic_ascii(self):
        assert sanitize_workspace_name("my-project") == "my-project"

    def test_uppercase(self):
        assert sanitize_workspace_name("My-Project") == "my-project"

    def test_spaces(self):
        assert sanitize_workspace_name("my project") == "my-project"

    def test_chinese(self):
        result = sanitize_workspace_name("我的项目")
        assert result == "workspace"  # 纯非 ASCII → 全变连字符 → strip 后空 → fallback

    def test_mixed_chinese_ascii(self):
        result = sanitize_workspace_name("项目-app")
        assert result == "app"

    def test_special_chars(self):
        assert sanitize_workspace_name("my@project!v2") == "my-project-v2"

    def test_consecutive_hyphens(self):
        assert sanitize_workspace_name("a---b") == "a-b"

    def test_leading_trailing_hyphens(self):
        assert sanitize_workspace_name("-hello-") == "hello"

    def test_only_symbols(self):
        assert sanitize_workspace_name("@#$%") == "workspace"

    def test_empty_string(self):
        assert sanitize_workspace_name("") == "workspace"

    def test_numbers(self):
        assert sanitize_workspace_name("123") == "123"

    def test_dots_and_underscores(self):
        assert sanitize_workspace_name("my_project.v2") == "my-project-v2"


# ---------------------------------------------------------------------------
# WorkspaceManager.create 名称唯一性测试
# ---------------------------------------------------------------------------

class TestWorkspaceManagerNameUniqueness:
    def test_create_basic(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws = wm.create("My Project")
            assert ws.name == "my-project"

    def test_create_duplicate_name(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws1 = wm.create("test")
            ws2 = wm.create("test")
            assert ws1.name == "test"
            assert ws2.name == "test-1"

    def test_create_triple_duplicate(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws1 = wm.create("demo")
            ws2 = wm.create("demo")
            ws3 = wm.create("demo")
            assert ws1.name == "demo"
            assert ws2.name == "demo-1"
            assert ws3.name == "demo-2"

    def test_create_sanitizes_name(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws = wm.create("My Cool Project!")
            assert ws.name == "my-cool-project"


# ---------------------------------------------------------------------------
# WorkspaceManager.get_by_name 测试
# ---------------------------------------------------------------------------

class TestWorkspaceManagerGetByName:
    def test_get_existing(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws = wm.create("test-project")
            found = wm.get_by_name("test-project")
            assert found is not None
            assert found.id == ws.id

    def test_get_nonexistent(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            assert wm.get_by_name("nonexistent") is None

    def test_get_after_sanitize(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws = wm.create("My Project")
            assert wm.get_by_name("my-project") is not None
            assert wm.get_by_name("My Project") is None  # 原名称不匹配


# ---------------------------------------------------------------------------
# App RPC handlers 测试
# ---------------------------------------------------------------------------

def _make_app_context(workspace_manager=None) -> RpcContext:
    async def noop(data: dict) -> None:
        pass
    return RpcContext(
        workspace_id="",
        broadcast=noop,
        workspace_manager=workspace_manager,
    )


@pytest.mark.asyncio
class TestAppWorkspaceList:
    async def test_empty_list(self):
        wm = WorkspaceManager()
        ctx = _make_app_context(wm)
        ops = WorkspaceOps()
        result = await ops.list({}, ctx)
        assert result == []

    async def test_with_workspaces(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            wm.create("test-a")
            wm.create("test-b")
            ctx = _make_app_context(wm)
            ops = WorkspaceOps()
            result = await ops.list({}, ctx)
            assert len(result) == 2
            names = {ws["name"] for ws in result}
            assert names == {"test-a", "test-b"}


@pytest.mark.asyncio
class TestAppWorkspaceCreate:
    async def test_create_success(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ctx = _make_app_context(wm)
            ops = WorkspaceOps()
            result = await ops.create({"name": "test-project"}, ctx)
            assert "error" not in result
            assert result["name"] == "test-project"

    async def test_missing_name(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ctx = _make_app_context(wm)
            ops = WorkspaceOps()
            result = await ops.create({}, ctx)
            assert "error" in result

    async def test_create_with_custom_name(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ctx = _make_app_context(wm)
            ops = WorkspaceOps()
            result = await ops.create({"name": "Custom Name"}, ctx)
            assert result["name"] == "custom-name"


# ---------------------------------------------------------------------------
# 注册表 (registry) 读写测试
# ---------------------------------------------------------------------------

class TestWorkspaceRegistry:
    """storage.load_workspace_registry / save_workspace_registry"""

    def test_load_nonexistent(self, tmp_path):
        """registry.json 不存在时返回空列表"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            result = storage.load_workspace_registry()
            assert result == []

    def test_save_and_load(self, tmp_path):
        """写入后再读取"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            ids = ["abc123", "def456"]
            storage.save_workspace_registry(ids)
            result = storage.load_workspace_registry()
            assert result == ids

    def test_corrupt_registry(self, tmp_path):
        """registry.json 损坏时返回空列表"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            ws_dir = tmp_path / "workspaces"
            ws_dir.mkdir(parents=True)
            (ws_dir / "registry.json").write_text("not json", encoding="utf-8")
            result = storage.load_workspace_registry()
            assert result == []

    def test_registry_missing_key(self, tmp_path):
        """registry.json 缺少 workspaces 键时返回空列表"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            ws_dir = tmp_path / "workspaces"
            ws_dir.mkdir(parents=True)
            (ws_dir / "registry.json").write_text('{"other": 1}', encoding="utf-8")
            result = storage.load_workspace_registry()
            assert result == []


# ---------------------------------------------------------------------------
# WorkspaceManager 注册表集成测试
# ---------------------------------------------------------------------------

class TestWorkspaceManagerRegistry:
    """WorkspaceManager 与 registry 集成"""

    def test_create_adds_to_registry(self, tmp_path):
        """create() 后 ID 出现在注册表"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws = wm.create("test")
            registry = storage.load_workspace_registry()
            assert ws.id in registry

    def test_create_multiple_registry_order(self, tmp_path):
        """多次 create()，最新的在注册表最前面"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws1 = wm.create("first")
            ws2 = wm.create("second")
            registry = storage.load_workspace_registry()
            assert registry[0] == ws2.id
            assert registry[1] == ws1.id

    def test_remove_from_registry(self, tmp_path):
        """remove() 后 ID 从注册表和内存消失，但 JSON 文件保留"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws = wm.create("test")
            ws_id = ws.id
            # 新格式文件名：{date}-{name}-{id}.json
            ws_dir = tmp_path / "workspaces"
            json_files = list(ws_dir.glob(f"*{ws_id}.json"))
            assert len(json_files) == 1

            result = wm.remove(ws_id)
            assert result is True
            assert wm.get(ws_id) is None
            assert ws_id not in storage.load_workspace_registry()
            assert json_files[0].exists()  # 文件保留

    def test_remove_nonexistent(self, tmp_path):
        """remove() 不存在的 ID 返回 False"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            assert wm.remove("nonexistent") is False

    def test_load_from_disk_with_registry(self, tmp_path):
        """load_from_disk() 只加载注册表中的 workspace"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            # 创建两个 workspace
            wm1 = WorkspaceManager()
            ws1 = wm1.create("one")
            ws2 = wm1.create("two")

            # 手动往 workspaces 目录写一个不在注册表中的文件
            extra_data = {
                "id": "extra999", "name": "extra",
                "sessions": [], "layout": None,
                "created_at": "", "updated_at": "", "last_accessed_at": "",
            }
            storage.save_workspace(extra_data)

            # 新实例 load，应只有注册表中的两个
            wm2 = WorkspaceManager()
            wm2.load_from_disk()
            assert wm2.get(ws1.id) is not None
            assert wm2.get(ws2.id) is not None
            assert wm2.get("extra999") is None

    def test_load_from_disk_empty_registry(self, tmp_path):
        """registry.json 不存在时 load_from_disk() 返回空"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            wm.load_from_disk()
            assert wm.list_all() == []

    def test_load_from_disk_cleans_invalid_ids(self, tmp_path):
        """注册表中的无效 ID（JSON 文件不存在）被自动清理"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            # 创建一个 workspace
            wm1 = WorkspaceManager()
            ws = wm1.create("real")

            # 手动往注册表中插入一个无效 ID
            registry = storage.load_workspace_registry()
            registry.append("nonexistent999")
            storage.save_workspace_registry(registry)

            # 新实例 load，无效 ID 应被清理
            wm2 = WorkspaceManager()
            wm2.load_from_disk()
            assert wm2.get(ws.id) is not None
            assert wm2.get("nonexistent999") is None

            cleaned = storage.load_workspace_registry()
            assert "nonexistent999" not in cleaned
            assert ws.id in cleaned


# ---------------------------------------------------------------------------
# workspace.remove RPC 测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAppWorkspaceRemove:
    async def test_remove_success(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws = wm.create("test")
            ctx = _make_app_context(wm)
            ops = WorkspaceOps()
            result = await ops.remove({"workspace_id": ws.id}, ctx)
            assert result == {"ok": True}
            assert wm.get(ws.id) is None

    async def test_remove_missing_id(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ctx = _make_app_context(wm)
            ops = WorkspaceOps()
            result = await ops.remove({}, ctx)
            assert "error" in result

    async def test_remove_nonexistent(self, tmp_path):
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ctx = _make_app_context(wm)
            ops = WorkspaceOps()
            result = await ops.remove(
                {"workspace_id": "nonexistent"}, ctx
            )
            assert "error" in result
