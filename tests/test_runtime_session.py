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
    get_session_class,
    _LEGACY_TYPE_MAP,
)
from mutbot.runtime.session_impl import (
    SessionManager,
    SessionRuntime,
    AgentSessionRuntime,
    _session_from_dict,
)


# ---------------------------------------------------------------------------
# Session Declaration 类层次
# ---------------------------------------------------------------------------

class TestSessionHierarchy:
    """Session 基类与子类的继承关系"""

    def test_session_is_declaration(self):
        assert issubclass(Session, mutobj.Declaration)

    def test_agent_session_inherits_session(self):
        assert issubclass(AgentSession, Session)

    def test_terminal_session_inherits_session(self):
        assert issubclass(TerminalSession, Session)

    def test_document_session_inherits_session(self):
        assert issubclass(DocumentSession, Session)

    def test_agent_session_type_auto_generated(self):
        s = AgentSession(id="a", workspace_id="w", title="t")
        assert s.type == "mutbot.session.AgentSession"

    def test_terminal_session_type_auto_generated(self):
        s = TerminalSession(id="a", workspace_id="w", title="t")
        assert s.type == "mutbot.session.TerminalSession"

    def test_document_session_type_auto_generated(self):
        s = DocumentSession(id="a", workspace_id="w", title="t")
        assert s.type == "mutbot.session.DocumentSession"

    def test_agent_session_extra_fields(self):
        s = AgentSession(id="a", workspace_id="w", title="t",
                         model="gpt-4", system_prompt="hello")
        assert s.model == "gpt-4"
        assert s.system_prompt == "hello"

    def test_document_session_extra_fields(self):
        s = DocumentSession(id="a", workspace_id="w", title="t",
                            file_path="/tmp/x.py", language="python")
        assert s.file_path == "/tmp/x.py"
        assert s.language == "python"

    def test_session_base_defaults(self):
        s = AgentSession(id="a", workspace_id="w", title="t")
        assert s.status == "active"
        assert s.created_at == ""
        assert s.updated_at == ""
        assert s.config == {}
        assert s.deleted is False


# ---------------------------------------------------------------------------
# Config 字段隔离（mutable default）
# ---------------------------------------------------------------------------

class TestConfigIsolation:
    """config: dict 使用 field(default_factory=dict)，实例间不共享"""

    def test_config_not_shared(self):
        s1 = AgentSession(id="a", workspace_id="w", title="t")
        s2 = AgentSession(id="b", workspace_id="w", title="t")
        s1.config["key"] = "value"
        assert s2.config == {}

    def test_config_custom_value(self):
        s = AgentSession(id="a", workspace_id="w", title="t",
                         config={"model": "test"})
        assert s.config == {"model": "test"}


# ---------------------------------------------------------------------------
# 类型注册表（全限定名模式）
# ---------------------------------------------------------------------------

class TestTypeRegistry:
    """基于 mutobj.discover_subclasses + 全限定名的类型查找"""

    def test_get_session_class_by_qualified_name(self):
        assert get_session_class("mutbot.session.AgentSession") is AgentSession
        assert get_session_class("mutbot.session.TerminalSession") is TerminalSession
        assert get_session_class("mutbot.session.DocumentSession") is DocumentSession

    def test_get_session_class_legacy_names(self):
        """旧短名称通过 _LEGACY_TYPE_MAP 兼容"""
        assert get_session_class("agent") is AgentSession
        assert get_session_class("terminal") is TerminalSession
        assert get_session_class("document") is DocumentSession

    def test_get_session_class_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown session type"):
            get_session_class("nonexistent")

    def test_legacy_type_map_covers_builtins(self):
        assert "agent" in _LEGACY_TYPE_MAP
        assert "terminal" in _LEGACY_TYPE_MAP
        assert "document" in _LEGACY_TYPE_MAP

    def test_discover_subclasses_includes_all(self):
        subs = mutobj.discover_subclasses(Session)
        names = {cls.__name__ for cls in subs}
        assert names >= {"AgentSession", "TerminalSession", "DocumentSession"}


# ---------------------------------------------------------------------------
# 序列化 / 反序列化
# ---------------------------------------------------------------------------

class TestSerialization:
    """Session.serialize() 和 _session_from_dict() 往返测试"""

    def test_agent_session_serialize(self):
        s = AgentSession(
            id="abc", workspace_id="ws1", title="Test",
            status="active", config={"k": "v"},
        )
        d = s.serialize()
        assert d["id"] == "abc"
        assert d["type"] == "mutbot.session.AgentSession"
        assert d["config"] == {"k": "v"}
        assert d["deleted"] is False

    def test_terminal_session_serialize(self):
        s = TerminalSession(
            id="xyz", workspace_id="ws1", title="Term",
        )
        d = s.serialize()
        assert d["type"] == "mutbot.session.TerminalSession"

    def test_serialize_roundtrip_agent(self):
        original = AgentSession(
            id="rt1", workspace_id="ws1", title="RT Test",
            status="active", config={"model": "gpt-4"},
            created_at="2026-01-01", updated_at="2026-01-02",
        )
        data = original.serialize()
        restored = _session_from_dict(data)
        assert isinstance(restored, AgentSession)
        assert restored.id == original.id
        assert restored.type == "mutbot.session.AgentSession"
        assert restored.config == {"model": "gpt-4"}
        assert restored.status == "active"
        assert restored.created_at == "2026-01-01"

    def test_serialize_roundtrip_terminal(self):
        original = TerminalSession(
            id="rt2", workspace_id="ws1", title="Term",
            config={"terminal_id": "tid1"},
        )
        data = original.serialize()
        restored = _session_from_dict(data)
        assert isinstance(restored, TerminalSession)
        assert restored.config["terminal_id"] == "tid1"

    def test_serialize_roundtrip_document(self):
        original = DocumentSession(
            id="rt3", workspace_id="ws1", title="Doc",
        )
        data = original.serialize()
        restored = _session_from_dict(data)
        assert isinstance(restored, DocumentSession)


# ---------------------------------------------------------------------------
# 旧持久化格式向后兼容
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """从旧 dataclass 格式的 JSON 正确反序列化（旧短名称 → 全限定名）"""

    def test_old_agent_session_data(self):
        old_data = {
            "id": "abc123",
            "workspace_id": "ws1",
            "title": "Agent 1",
            "type": "agent",
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "config": {"model": "test"},
            "deleted": False,
        }
        session = _session_from_dict(old_data)
        assert isinstance(session, AgentSession)
        assert session.id == "abc123"
        # 旧短名称 "agent" 被映射为全限定名
        assert session.type == "mutbot.session.AgentSession"
        assert session.config == {"model": "test"}

    def test_old_terminal_session_data(self):
        old_data = {
            "id": "def456",
            "workspace_id": "ws1",
            "title": "Terminal 1",
            "type": "terminal",
            "status": "active",
            "config": {"terminal_id": "tid1", "shell_command": "cmd.exe"},
        }
        session = _session_from_dict(old_data)
        assert isinstance(session, TerminalSession)
        assert session.type == "mutbot.session.TerminalSession"
        assert session.config["terminal_id"] == "tid1"

    def test_old_document_session_data(self):
        old_data = {
            "id": "ghi789",
            "workspace_id": "ws1",
            "title": "Document 1",
            "type": "document",
            "status": "active",
            "config": {},
        }
        session = _session_from_dict(old_data)
        assert isinstance(session, DocumentSession)
        assert session.type == "mutbot.session.DocumentSession"

    def test_unknown_type_falls_back_to_session(self):
        data = {"id": "u1", "type": "unknown", "workspace_id": "w", "title": "t"}
        session = _session_from_dict(data)
        # 未知类型回退到基类 Session
        assert type(session) is Session
        assert session.type == "unknown"

    def test_missing_fields_use_defaults(self):
        """旧数据缺少字段时使用默认值"""
        data = {"id": "m1", "workspace_id": "w", "title": "t", "type": "agent"}
        session = _session_from_dict(data)
        assert session.status == "ended"  # _session_from_dict default
        assert session.config == {}
        assert session.deleted is False

    def test_none_config_becomes_empty_dict(self):
        """旧数据 config=None 转换为空 dict"""
        data = {
            "id": "n1", "workspace_id": "w", "title": "t",
            "type": "agent", "config": None,
        }
        session = _session_from_dict(data)
        assert session.config == {}


# ---------------------------------------------------------------------------
# Runtime 分离模式
# ---------------------------------------------------------------------------

class TestRuntimeSeparation:
    """SessionRuntime / AgentSessionRuntime 数据类"""

    def test_session_runtime_base(self):
        rt = SessionRuntime()
        assert rt is not None

    def test_agent_session_runtime_defaults(self):
        rt = AgentSessionRuntime()
        assert rt.agent is None
        assert rt.bridge is None

    def test_agent_session_runtime_with_values(self):
        sentinel = object()
        rt = AgentSessionRuntime(agent=sentinel)
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

    def test_create_agent_session(self):
        sm = self._make_manager()
        session = sm.create("ws1", session_type="mutbot.session.AgentSession")
        assert isinstance(session, AgentSession)
        assert session.type == "mutbot.session.AgentSession"
        assert session.workspace_id == "ws1"
        assert session.title == "Agent 1"

    def test_create_agent_session_legacy_type(self):
        """旧短名称仍然可用"""
        sm = self._make_manager()
        session = sm.create("ws1", session_type="agent")
        assert isinstance(session, AgentSession)
        assert session.type == "mutbot.session.AgentSession"

    def test_create_terminal_session(self):
        sm = self._make_manager()
        session = sm.create("ws1", session_type="mutbot.session.TerminalSession")
        assert isinstance(session, TerminalSession)
        assert session.type == "mutbot.session.TerminalSession"
        assert session.title == "Terminal 1"

    def test_create_document_session(self):
        sm = self._make_manager()
        session = sm.create("ws1", session_type="mutbot.session.DocumentSession")
        assert isinstance(session, DocumentSession)
        assert session.type == "mutbot.session.DocumentSession"

    def test_create_auto_increment_title(self):
        sm = self._make_manager()
        s1 = sm.create("ws1", session_type="mutbot.session.AgentSession")
        s2 = sm.create("ws1", session_type="mutbot.session.AgentSession")
        assert s1.title == "Agent 1"
        assert s2.title == "Agent 2"

    def test_get_session(self):
        sm = self._make_manager()
        created = sm.create("ws1")
        found = sm.get(created.id)
        assert found is created

    def test_get_nonexistent_returns_none(self):
        sm = self._make_manager()
        assert sm.get("nonexistent") is None

    def test_list_by_workspace(self):
        sm = self._make_manager()
        sm.create("ws1")
        sm.create("ws1")
        sm.create("ws2")
        assert len(sm.list_by_workspace("ws1")) == 2
        assert len(sm.list_by_workspace("ws2")) == 1

    def test_list_excludes_deleted(self):
        sm = self._make_manager()
        s = sm.create("ws1")
        sm.delete(s.id)
        assert len(sm.list_by_workspace("ws1")) == 0

    def test_update_title(self):
        sm = self._make_manager()
        s = sm.create("ws1")
        updated = sm.update(s.id, title="New Title")
        assert updated is not None
        assert updated.title == "New Title"

    def test_update_config_merges(self):
        sm = self._make_manager()
        s = sm.create("ws1", config={"a": 1})
        sm.update(s.id, config={"b": 2})
        assert s.config == {"a": 1, "b": 2}

    def test_update_nonexistent_returns_none(self):
        sm = self._make_manager()
        assert sm.update("nonexistent", title="x") is None

    def test_delete_soft_delete(self):
        sm = self._make_manager()
        s = sm.create("ws1")
        assert sm.delete(s.id) is True
        assert s.deleted is True
        # 仍可通过 get 获取
        assert sm.get(s.id) is s

    def test_delete_nonexistent_returns_false(self):
        sm = self._make_manager()
        assert sm.delete("nonexistent") is False

    def test_get_runtime_empty(self):
        sm = self._make_manager()
        sm.create("ws1")
        assert sm.get_runtime("nonexistent") is None

    def test_get_agent_runtime_none_initially(self):
        sm = self._make_manager()
        s = sm.create("ws1")
        assert sm.get_agent_runtime(s.id) is None

    def test_create_with_config(self):
        sm = self._make_manager()
        s = sm.create("ws1", config={"terminal_id": "t1"})
        assert s.config == {"terminal_id": "t1"}
