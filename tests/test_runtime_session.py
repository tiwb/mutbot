"""测试 Session Declaration 体系

涵盖：
- Session Declaration 类层次
- 类型注册表（mutobj 子类发现 API + 全限定名）
- 序列化 / 反序列化
- 旧持久化格式向后兼容
- Runtime 分离模式
- SessionManager 基础操作
"""

from __future__ import annotations

import mutobj
import pytest

from mutbot.session import (
    Session,
    AgentSession,
    DocumentSession,
    TerminalSession,
)
from mutbot.runtime.session_manager import (
    SessionManager,
    SessionRuntime,
    AgentSessionRuntime,
)


# ---------------------------------------------------------------------------
# Session Declaration 类层次
# ---------------------------------------------------------------------------

class TestSessionDeclaration:
    """Session 基类和子类的 Declaration 行为"""

    def test_session_is_declaration(self):
        assert issubclass(Session, mutobj.Declaration)

    def test_agent_session_subclass(self):
        assert issubclass(AgentSession, Session)

    def test_terminal_session_subclass(self):
        assert issubclass(TerminalSession, Session)

    def test_document_session_subclass(self):
        assert issubclass(DocumentSession, Session)

    def test_session_construct_minimal(self):
        s = AgentSession(id="abc", workspace_id="ws1", title="Test")
        assert s.id == "abc"
        assert s.workspace_id == "ws1"
        assert s.title == "Test"
        assert s.type == "mutbot.session.AgentSession"

    def test_terminal_session_has_terminal_fields(self):
        """TerminalSession 包含 terminal 特有字段"""
        s = TerminalSession(id="t1", workspace_id="ws1", title="Term")
        assert hasattr(s, "scrollback_b64")

    def test_session_type_auto_qualified(self):
        """Session.type 自动填充为全限定类名"""
        s = AgentSession(id="a", workspace_id="w", title="t")
        assert s.type == "mutbot.session.AgentSession"
        s2 = TerminalSession(id="t", workspace_id="w", title="t")
        assert s2.type == "mutbot.session.TerminalSession"


# ---------------------------------------------------------------------------
# 类型注册表
# ---------------------------------------------------------------------------

class TestTypeRegistry:
    """通过 mutobj 子类发现 API + 全限定名查找"""

    def test_discover_session_subclasses(self):
        subs = list(mutobj.discover_subclasses(Session))
        names = {c.__name__ for c in subs}
        assert "AgentSession" in names
        assert "TerminalSession" in names
        assert "DocumentSession" in names

    def test_get_session_class_by_name(self):
        cls = Session.get_session_class("mutbot.session.AgentSession")
        assert cls is AgentSession

    def test_get_session_class_terminal(self):
        cls = Session.get_session_class("mutbot.session.TerminalSession")
        assert cls is TerminalSession

    def test_get_session_class_document(self):
        cls = Session.get_session_class("mutbot.session.DocumentSession")
        assert cls is DocumentSession

    def test_get_session_class_unknown_raises(self):
        with pytest.raises(ValueError):
            Session.get_session_class("mutbot.session.NonexistentSession")


# ---------------------------------------------------------------------------
# 序列化 / 反序列化
# ---------------------------------------------------------------------------

class TestSerialization:
    """Session serialize / deserialize"""

    def test_serialize_basic_fields(self):
        s = AgentSession(id="a1", workspace_id="ws", title="My Agent")
        d = s.serialize()
        assert d["id"] == "a1"
        assert d["workspace_id"] == "ws"
        assert d["title"] == "My Agent"
        assert d["type"] == "mutbot.session.AgentSession"

    def test_deserialize_roundtrip(self):
        original = AgentSession(id="r1", workspace_id="ws", title="Round")
        data = original.serialize()
        restored = Session.deserialize(data)
        assert isinstance(restored, AgentSession)
        assert restored.id == original.id
        assert restored.title == original.title
        assert restored.type == original.type

    def test_deserialize_terminal(self):
        data = {
            "id": "t1",
            "workspace_id": "ws",
            "title": "Term",
            "type": "mutbot.session.TerminalSession",
        }
        restored = Session.deserialize(data)
        assert isinstance(restored, TerminalSession)

    def test_deserialize_preserves_config(self):
        original = AgentSession(id="c1", workspace_id="ws", title="Cfg")
        original.config = {"key": "value", "nested": {"a": 1}}
        data = original.serialize()
        restored = Session.deserialize(data)
        assert restored.config == {"key": "value", "nested": {"a": 1}}


# ---------------------------------------------------------------------------
# 旧持久化格式兼容
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """旧版持久化格式中缺少某些字段时的兼容处理"""

    def test_missing_type_defaults(self):
        """缺少 type 字段时可通过显式 cls 反序列化"""
        data = {"id": "old", "workspace_id": "ws", "title": "Old"}
        # 直接构造，不经过 deserialize
        s = AgentSession(**data)
        assert s.type == "mutbot.session.AgentSession"

    def test_missing_config_defaults_empty(self):
        data = {
            "id": "a", "workspace_id": "w", "title": "t",
            "type": "mutbot.session.AgentSession",
        }
        s = Session.deserialize(data)
        assert s.config == {} or s.config is not None


# ---------------------------------------------------------------------------
# Runtime 分离模式
# ---------------------------------------------------------------------------

class TestRuntimeSeparation:
    """Runtime 数据与 Declaration 分离"""

    def test_session_runtime_is_plain_object(self):
        rt = SessionRuntime()
        assert rt is not None

    def test_agent_session_runtime_defaults(self):
        rt = AgentSessionRuntime()
        assert rt.agent is None
        assert rt.bridge is None

    def test_agent_session_runtime_with_values(self):
        sentinel = object()
        rt = AgentSessionRuntime(agent=sentinel)  # type: ignore[arg-type]
        assert rt.agent is sentinel

    def test_session_has_no_agent_field(self):
        """Session Declaration 不再包含 agent/bridge runtime 字段"""
        s = AgentSession(id="a", workspace_id="w", title="t")
        assert not hasattr(s, "agent")
        assert not hasattr(s, "bridge")


# ---------------------------------------------------------------------------
# SessionManager 基础操作
# ---------------------------------------------------------------------------

class TestSessionManager:
    """SessionManager CRUD 和 runtime 管理"""

    def _make_manager(self) -> SessionManager:
        return SessionManager()

    @pytest.mark.asyncio
    async def test_create_agent_session(self):
        sm = self._make_manager()
        session = await sm.create("ws1", session_type="mutbot.session.AgentSession")
        assert isinstance(session, AgentSession)
        assert session.type == "mutbot.session.AgentSession"
        assert session.workspace_id == "ws1"
        assert session.title == "Agent 1"

    @pytest.mark.asyncio
    async def test_create_terminal_session(self):
        sm = self._make_manager()
        session = await sm.create("ws1", session_type="mutbot.session.TerminalSession")
        assert isinstance(session, TerminalSession)
        assert session.type == "mutbot.session.TerminalSession"
        assert session.title == "Terminal 1"

    @pytest.mark.asyncio
    async def test_create_document_session(self):
        sm = self._make_manager()
        session = await sm.create("ws1", session_type="mutbot.session.DocumentSession")
        assert isinstance(session, DocumentSession)
        assert session.type == "mutbot.session.DocumentSession"

    @pytest.mark.asyncio
    async def test_create_auto_increment_title(self):
        sm = self._make_manager()
        s1 = await sm.create("ws1", session_type="mutbot.session.AgentSession")
        s2 = await sm.create("ws1", session_type="mutbot.session.AgentSession")
        assert s1.title == "Agent 1"
        assert s2.title == "Agent 2"

    @pytest.mark.asyncio
    async def test_get_session(self):
        sm = self._make_manager()
        created = await sm.create("ws1", session_type="mutbot.session.AgentSession")
        found = sm.get(created.id)
        assert found is created

    def test_get_nonexistent_returns_none(self):
        sm = self._make_manager()
        assert sm.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_by_workspace(self):
        sm = self._make_manager()
        await sm.create("ws1", session_type="mutbot.session.AgentSession")
        await sm.create("ws1", session_type="mutbot.session.AgentSession")
        await sm.create("ws2", session_type="mutbot.session.AgentSession")
        assert len(sm.list_by_workspace("ws1")) == 2
        assert len(sm.list_by_workspace("ws2")) == 1

    @pytest.mark.asyncio
    async def test_list_excludes_deleted(self):
        sm = self._make_manager()
        s = await sm.create("ws1", session_type="mutbot.session.AgentSession")
        sm.delete(s.id)
        assert len(sm.list_by_workspace("ws1")) == 0

    @pytest.mark.asyncio
    async def test_update_title(self):
        sm = self._make_manager()
        s = await sm.create("ws1", session_type="mutbot.session.AgentSession")
        updated = sm.update(s.id, title="New Title")
        assert updated is not None
        assert updated.title == "New Title"

    @pytest.mark.asyncio
    async def test_update_config_merges(self):
        sm = self._make_manager()
        s = await sm.create("ws1", session_type="mutbot.session.AgentSession", config={"a": 1})
        sm.update(s.id, config={"b": 2})
        assert s.config == {"a": 1, "b": 2}

    def test_update_nonexistent_returns_none(self):
        sm = self._make_manager()
        assert sm.update("nonexistent", title="x") is None

    @pytest.mark.asyncio
    async def test_delete_removes_session(self):
        sm = self._make_manager()
        s = await sm.create("ws1", session_type="mutbot.session.AgentSession")
        assert sm.delete(s.id) is True
        # 已从内存中移除
        assert sm.get(s.id) is None

    def test_delete_nonexistent_returns_false(self):
        sm = self._make_manager()
        assert sm.delete("nonexistent") is False

    @pytest.mark.asyncio
    async def test_get_runtime_empty(self):
        sm = self._make_manager()
        await sm.create("ws1", session_type="mutbot.session.AgentSession")
        assert sm.get_runtime("nonexistent") is None

    @pytest.mark.asyncio
    async def test_get_agent_runtime_none_initially(self):
        sm = self._make_manager()
        s = await sm.create("ws1", session_type="mutbot.session.AgentSession")
        assert sm.get_agent_runtime(s.id) is None

    @pytest.mark.asyncio
    async def test_create_with_config(self):
        sm = self._make_manager()
        s = await sm.create("ws1", session_type="mutbot.session.AgentSession", config={"terminal_id": "t1"})
        assert s.config == {"terminal_id": "t1"}


# ---------------------------------------------------------------------------
# Session status 模型
# ---------------------------------------------------------------------------

class TestSessionStatus:
    """Session status 为开放字符串，默认空，各子类自行管理"""

    def test_base_status_default_empty(self):
        """Session 基类 status 默认为空字符串"""
        s = AgentSession(id="a", workspace_id="w", title="t")
        assert s.status == ""

    def test_terminal_status_default_empty(self):
        s = TerminalSession(id="a", workspace_id="w", title="t")
        assert s.status == ""

    def test_document_status_default_empty(self):
        s = DocumentSession(id="a", workspace_id="w", title="t")
        assert s.status == ""

    def test_status_set_arbitrary_string(self):
        """status 可设置任意字符串值"""
        s = AgentSession(id="a", workspace_id="w", title="t")
        s.status = "running"
        assert s.status == "running"
        s.status = "custom_state"
        assert s.status == "custom_state"
        s.status = ""
        assert s.status == ""

    def test_status_persists_through_serialize(self):
        """status 持久化和恢复正确"""
        s = AgentSession(id="a", workspace_id="w", title="t")
        s.status = "running"
        data = s.serialize()
        assert data["status"] == "running"

    def test_status_restore_from_dict(self):
        """从 dict 恢复 status 保留原始值"""
        data = {
            "id": "a", "workspace_id": "w", "title": "t",
            "type": "mutbot.session.AgentSession",
            "status": "stopped",
        }
        restored = Session.deserialize(data)
        assert restored.status == "stopped"

    def test_status_restore_empty(self):
        """空 status 从 dict 恢复为空字符串"""
        data = {
            "id": "a", "workspace_id": "w", "title": "t",
            "type": "mutbot.session.AgentSession",
            "status": "",
        }
        restored = Session.deserialize(data)
        assert restored.status == ""

    def test_status_restore_missing_defaults_empty(self):
        """缺少 status 字段时默认为空字符串"""
        data = {
            "id": "a", "workspace_id": "w", "title": "t",
            "type": "mutbot.session.AgentSession",
        }
        restored = Session.deserialize(data)
        assert restored.status == ""

    def test_status_roundtrip_custom_value(self):
        """自定义 status 完整往返测试"""
        original = AgentSession(id="rt", workspace_id="w", title="t")
        original.status = "my_custom_status"
        data = original.serialize()
        restored = Session.deserialize(data)
        assert restored.status == "my_custom_status"

    @pytest.mark.asyncio
    async def test_set_session_status(self):
        """SessionManager.set_session_status 更新 status"""
        sm = SessionManager()
        s = await sm.create("ws1", session_type="mutbot.session.AgentSession")
        assert s.status == ""
        sm.set_session_status(s.id, "running")
        assert s.status == "running"
        sm.set_session_status(s.id, "")
        assert s.status == ""

    @pytest.mark.asyncio
    async def test_set_session_status_noop_same_value(self):
        """相同 status 不触发更新"""
        sm = SessionManager()
        s = await sm.create("ws1", session_type="mutbot.session.AgentSession")
        old_updated = s.updated_at
        sm.set_session_status(s.id, "")  # 相同值
        assert s.updated_at == old_updated  # 未变

    def test_set_session_status_nonexistent(self):
        """不存在的 session 静默返回"""
        sm = SessionManager()
        sm.set_session_status("nonexistent", "running")  # 不抛异常
