"""测试工作区选择器后端功能

涵盖：
- sanitize_workspace_name 各种输入
- 工作区名称唯一性（重复名称自动加后缀）
- get_by_name 查找
- 注册表（registry）读写与 WorkspaceManager 注册表集成
- /ws/app RPC handlers (workspace.list / workspace.create / filesystem.browse / workspace.remove)
- Origin 校验
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from mutbot.runtime.workspace import WorkspaceManager, sanitize_workspace_name
from mutbot.runtime import storage
from mutbot.web.rpc import RpcContext
from mutbot.web.routes import (
    _check_ws_origin,
    handle_app_workspace_list,
    handle_app_workspace_create,
    handle_app_workspace_remove,
    handle_filesystem_browse,
)


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
    def setup_method(self):
        self.wm = WorkspaceManager()

    def test_create_basic(self):
        ws = self.wm.create("My Project", "/tmp/test")
        assert ws.name == "my-project"

    def test_create_duplicate_name(self):
        ws1 = self.wm.create("test", "/tmp/a")
        ws2 = self.wm.create("test", "/tmp/b")
        assert ws1.name == "test"
        assert ws2.name == "test-1"

    def test_create_triple_duplicate(self):
        ws1 = self.wm.create("demo", "/tmp/a")
        ws2 = self.wm.create("demo", "/tmp/b")
        ws3 = self.wm.create("demo", "/tmp/c")
        assert ws1.name == "demo"
        assert ws2.name == "demo-1"
        assert ws3.name == "demo-2"

    def test_create_sanitizes_name(self):
        ws = self.wm.create("My Cool Project!", "/tmp/test")
        assert ws.name == "my-cool-project"


# ---------------------------------------------------------------------------
# WorkspaceManager.get_by_name 测试
# ---------------------------------------------------------------------------

class TestWorkspaceManagerGetByName:
    def setup_method(self):
        self.wm = WorkspaceManager()

    def test_get_existing(self):
        ws = self.wm.create("test-project", "/tmp/test")
        found = self.wm.get_by_name("test-project")
        assert found is not None
        assert found.id == ws.id

    def test_get_nonexistent(self):
        assert self.wm.get_by_name("nonexistent") is None

    def test_get_after_sanitize(self):
        ws = self.wm.create("My Project", "/tmp/test")
        assert self.wm.get_by_name("my-project") is not None
        assert self.wm.get_by_name("My Project") is None  # 原名称不匹配


# ---------------------------------------------------------------------------
# Origin 校验测试
# ---------------------------------------------------------------------------

class TestOriginValidation:
    def test_no_origin(self):
        assert _check_ws_origin(None) is True

    def test_mutbot_ai_https(self):
        assert _check_ws_origin("https://mutbot.ai") is True

    def test_mutbot_ai_http(self):
        assert _check_ws_origin("http://mutbot.ai") is True

    def test_localhost(self):
        assert _check_ws_origin("http://localhost:8741") is True

    def test_localhost_no_port(self):
        assert _check_ws_origin("http://localhost") is True

    def test_127_0_0_1(self):
        assert _check_ws_origin("http://127.0.0.1:8741") is True

    def test_ipv6_localhost(self):
        assert _check_ws_origin("http://[::1]:8741") is True

    def test_random_origin_rejected(self):
        assert _check_ws_origin("https://evil.com") is False

    def test_subdomain_rejected(self):
        assert _check_ws_origin("https://sub.mutbot.ai") is False


# ---------------------------------------------------------------------------
# App RPC handlers 测试
# ---------------------------------------------------------------------------

def _make_app_context(workspace_manager=None) -> RpcContext:
    async def noop(data: dict) -> None:
        pass
    return RpcContext(
        workspace_id="",
        broadcast=noop,
        managers={"workspace_manager": workspace_manager},
    )


@pytest.mark.asyncio
class TestAppWorkspaceList:
    async def test_empty_list(self):
        wm = WorkspaceManager()
        ctx = _make_app_context(wm)
        result = await handle_app_workspace_list({}, ctx)
        assert result == []

    async def test_with_workspaces(self):
        wm = WorkspaceManager()
        wm.create("test-a", "/tmp/a")
        wm.create("test-b", "/tmp/b")
        ctx = _make_app_context(wm)
        result = await handle_app_workspace_list({}, ctx)
        assert len(result) == 2
        names = {ws["name"] for ws in result}
        assert names == {"test-a", "test-b"}


@pytest.mark.asyncio
class TestAppWorkspaceCreate:
    async def test_create_success(self, tmp_path):
        wm = WorkspaceManager()
        ctx = _make_app_context(wm)
        result = await handle_app_workspace_create(
            {"project_path": str(tmp_path)}, ctx
        )
        assert "error" not in result
        assert result["name"] == sanitize_workspace_name(tmp_path.name)
        assert result["project_path"] == str(tmp_path)

    async def test_missing_project_path(self):
        wm = WorkspaceManager()
        ctx = _make_app_context(wm)
        result = await handle_app_workspace_create({}, ctx)
        assert "error" in result

    async def test_relative_path_rejected(self, tmp_path):
        wm = WorkspaceManager()
        ctx = _make_app_context(wm)
        result = await handle_app_workspace_create(
            {"project_path": "relative/path"}, ctx
        )
        assert "error" in result

    async def test_nonexistent_path_rejected(self):
        wm = WorkspaceManager()
        ctx = _make_app_context(wm)
        result = await handle_app_workspace_create(
            {"project_path": "/nonexistent/path/12345"}, ctx
        )
        assert "error" in result

    async def test_create_with_custom_name(self, tmp_path):
        wm = WorkspaceManager()
        ctx = _make_app_context(wm)
        result = await handle_app_workspace_create(
            {"project_path": str(tmp_path), "name": "Custom Name"}, ctx
        )
        assert result["name"] == "custom-name"


@pytest.mark.asyncio
class TestFilesystemBrowse:
    async def test_home_directory(self):
        ctx = _make_app_context()
        result = await handle_filesystem_browse({}, ctx)
        assert "error" not in result
        assert result["path"] == str(Path.home())
        assert isinstance(result["entries"], list)

    async def test_specific_directory(self, tmp_path):
        # 创建子目录
        (tmp_path / "sub-a").mkdir()
        (tmp_path / "sub-b").mkdir()
        (tmp_path / "file.txt").write_text("hello")

        ctx = _make_app_context()
        result = await handle_filesystem_browse({"path": str(tmp_path)}, ctx)
        assert result["path"] == str(tmp_path.resolve())
        # 只返回目录，不返回文件
        names = [e["name"] for e in result["entries"]]
        assert "sub-a" in names
        assert "sub-b" in names
        assert "file.txt" not in names

    async def test_hidden_dirs_excluded(self, tmp_path):
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "visible").mkdir()

        ctx = _make_app_context()
        result = await handle_filesystem_browse({"path": str(tmp_path)}, ctx)
        names = [e["name"] for e in result["entries"]]
        assert "visible" in names
        assert ".hidden" not in names

    async def test_parent_field(self, tmp_path):
        sub = tmp_path / "child"
        sub.mkdir()
        ctx = _make_app_context()
        result = await handle_filesystem_browse({"path": str(sub)}, ctx)
        assert result["parent"] == str(tmp_path.resolve())

    async def test_nonexistent_directory(self):
        ctx = _make_app_context()
        result = await handle_filesystem_browse(
            {"path": "/nonexistent/path/12345"}, ctx
        )
        assert "error" in result

    async def test_entries_sorted(self, tmp_path):
        (tmp_path / "zebra").mkdir()
        (tmp_path / "alpha").mkdir()
        (tmp_path / "Middle").mkdir()

        ctx = _make_app_context()
        result = await handle_filesystem_browse({"path": str(tmp_path)}, ctx)
        names = [e["name"] for e in result["entries"]]
        assert names == sorted(names, key=str.lower)


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
            ws = wm.create("test", "/tmp/test")
            registry = storage.load_workspace_registry()
            assert ws.id in registry

    def test_create_multiple_registry_order(self, tmp_path):
        """多次 create()，最新的在注册表最前面"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws1 = wm.create("first", "/tmp/a")
            ws2 = wm.create("second", "/tmp/b")
            registry = storage.load_workspace_registry()
            assert registry[0] == ws2.id
            assert registry[1] == ws1.id

    def test_remove_from_registry(self, tmp_path):
        """remove() 后 ID 从注册表和内存消失，但 JSON 文件保留"""
        with mock.patch.object(storage, "MUTBOT_DIR", str(tmp_path)):
            wm = WorkspaceManager()
            ws = wm.create("test", "/tmp/test")
            ws_id = ws.id
            json_path = tmp_path / "workspaces" / f"{ws_id}.json"
            assert json_path.exists()

            result = wm.remove(ws_id)
            assert result is True
            assert wm.get(ws_id) is None
            assert ws_id not in storage.load_workspace_registry()
            assert json_path.exists()  # 文件保留

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
            ws1 = wm1.create("one", "/tmp/a")
            ws2 = wm1.create("two", "/tmp/b")

            # 手动往 workspaces 目录写一个不在注册表中的文件
            extra_data = {
                "id": "extra999", "name": "extra", "project_path": "/tmp/x",
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
            ws = wm1.create("real", "/tmp/a")

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
            ws = wm.create("test", "/tmp/test")
            ctx = _make_app_context(wm)
            result = await handle_app_workspace_remove(
                {"workspace_id": ws.id}, ctx
            )
            assert result == {"ok": True}
            assert wm.get(ws.id) is None

    async def test_remove_missing_id(self):
        wm = WorkspaceManager()
        ctx = _make_app_context(wm)
        result = await handle_app_workspace_remove({}, ctx)
        assert "error" in result

    async def test_remove_nonexistent(self):
        wm = WorkspaceManager()
        ctx = _make_app_context(wm)
        result = await handle_app_workspace_remove(
            {"workspace_id": "nonexistent"}, ctx
        )
        assert "error" in result
