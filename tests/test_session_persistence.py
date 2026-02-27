"""Session 消息持久化与恢复测试

涵盖：
- _persist 在有/无 runtime 时的行为
- messages 跨 persist 周期保留
- total_tokens 序列化/反序列化
- stop() 后 messages 不丢失
- 模拟 server 重启：load_from_disk → _persist 不丢 messages
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mutbot.session import AgentSession, Session
from mutbot.runtime.session_impl import (
    SessionManager,
    AgentSessionRuntime,
    _session_from_dict,
)
from mutbot.runtime import storage
from mutbot.web.serializers import serialize_message
from mutagent.messages import Message, ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """将 storage 重定向到临时目录。"""
    monkeypatch.setattr(storage, "MUTBOT_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def sm(storage_dir: Path) -> SessionManager:
    """创建使用临时存储的 SessionManager。"""
    return SessionManager()


def _make_messages() -> list[Message]:
    """构造包含 text + tool_call + tool_result 的典型 messages。"""
    return [
        Message(role="user", content="hello"),
        Message(
            role="assistant",
            content="Let me check.",
            tool_calls=[ToolCall(id="tc1", name="read_file", arguments={"path": "a.py"})],
        ),
        Message(
            role="user",
            tool_results=[ToolResult(tool_call_id="tc1", content="file content")],
        ),
        Message(role="assistant", content="Here is the result."),
    ]


def _read_session_json(storage_dir: Path, session_id: str) -> dict:
    """直接从磁盘读取 session JSON。"""
    for f in (storage_dir / "sessions").glob(f"*{session_id}*.json"):
        return json.loads(f.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"No JSON file for session {session_id}")


# ---------------------------------------------------------------------------
# total_tokens 序列化
# ---------------------------------------------------------------------------

class TestTotalTokensSerialization:
    """total_tokens 字段的序列化/反序列化"""

    def test_serialize_includes_total_tokens(self):
        s = AgentSession(id="a", workspace_id="w", title="t", total_tokens=12345)
        d = s.serialize()
        assert d["total_tokens"] == 12345

    def test_serialize_omits_zero_total_tokens(self):
        s = AgentSession(id="a", workspace_id="w", title="t", total_tokens=0)
        d = s.serialize()
        assert "total_tokens" not in d

    def test_deserialize_restores_total_tokens(self):
        data = {
            "id": "a", "workspace_id": "w", "title": "t",
            "type": "mutbot.session.AgentSession",
            "total_tokens": 99999,
        }
        s = _session_from_dict(data)
        assert isinstance(s, AgentSession)
        assert s.total_tokens == 99999

    def test_deserialize_missing_total_tokens_defaults_zero(self):
        data = {
            "id": "a", "workspace_id": "w", "title": "t",
            "type": "mutbot.session.AgentSession",
        }
        s = _session_from_dict(data)
        assert isinstance(s, AgentSession)
        assert s.total_tokens == 0


# ---------------------------------------------------------------------------
# _persist 基础行为
# ---------------------------------------------------------------------------

class TestPersistBasic:
    """_persist 在有/无 runtime 时的行为"""

    def test_persist_with_runtime_saves_messages(self, sm: SessionManager, storage_dir: Path):
        """有 runtime 时 messages 应写入 JSON"""
        session = sm.create("ws1", session_type="mutbot.session.AgentSession")
        messages = _make_messages()

        # 注入 runtime（模拟 agent 已启动）
        agent = MagicMock()
        agent.messages = messages
        sm._runtimes[session.id] = AgentSessionRuntime(agent=agent)

        sm._persist(session)

        data = _read_session_json(storage_dir, session.id)
        assert "messages" in data
        assert len(data["messages"]) == 4
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "hello"

    def test_persist_without_runtime_preserves_existing_messages(
        self, sm: SessionManager, storage_dir: Path,
    ):
        """无 runtime 时不应丢失磁盘上已有的 messages"""
        session = sm.create("ws1", session_type="mutbot.session.AgentSession")
        messages = _make_messages()

        # 第一次 persist：有 runtime，写入 messages
        agent = MagicMock()
        agent.messages = messages
        sm._runtimes[session.id] = AgentSessionRuntime(agent=agent)
        sm._persist(session)

        # 移除 runtime（模拟 server 重启后无 agent）
        sm._runtimes.pop(session.id)

        # 第二次 persist：无 runtime（如 update title）
        session.title = "Updated Title"
        sm._persist(session)

        data = _read_session_json(storage_dir, session.id)
        assert data["title"] == "Updated Title"
        assert len(data["messages"]) == 4, "messages 不应丢失"

    def test_persist_without_runtime_no_prior_messages(
        self, sm: SessionManager, storage_dir: Path,
    ):
        """无 runtime 且磁盘上也无 messages 时不应报错"""
        session = sm.create("ws1", session_type="mutbot.session.AgentSession")
        sm._persist(session)

        data = _read_session_json(storage_dir, session.id)
        assert "messages" not in data or data.get("messages") == []

    def test_persist_saves_total_tokens(self, sm: SessionManager, storage_dir: Path):
        """total_tokens 应随 persist 保存"""
        session = sm.create("ws1", session_type="mutbot.session.AgentSession")
        assert isinstance(session, AgentSession)
        session.total_tokens = 5000
        sm._persist(session)

        data = _read_session_json(storage_dir, session.id)
        assert data["total_tokens"] == 5000


# ---------------------------------------------------------------------------
# Messages 往返（round-trip）
# ---------------------------------------------------------------------------

class TestMessageRoundTrip:
    """消息序列化后从磁盘加载应完全一致"""

    def test_text_message_roundtrip(self, sm: SessionManager, storage_dir: Path):
        session = sm.create("ws1", session_type="mutbot.session.AgentSession")
        messages = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi there!"),
        ]

        agent = MagicMock()
        agent.messages = messages
        sm._runtimes[session.id] = AgentSessionRuntime(agent=agent)
        sm._persist(session)
        sm._runtimes.pop(session.id)

        loaded = sm._load_agent_messages(session.id)
        assert len(loaded) == 2
        assert loaded[0].role == "user"
        assert loaded[0].content == "Hello"
        assert loaded[1].role == "assistant"
        assert loaded[1].content == "Hi there!"

    def test_tool_call_roundtrip(self, sm: SessionManager, storage_dir: Path):
        session = sm.create("ws1", session_type="mutbot.session.AgentSession")
        messages = [
            Message(
                role="assistant",
                content="I'll search.",
                tool_calls=[ToolCall(id="tc1", name="search", arguments={"q": "test"})],
            ),
            Message(
                role="user",
                tool_results=[ToolResult(tool_call_id="tc1", content="found it", is_error=False)],
            ),
        ]

        agent = MagicMock()
        agent.messages = messages
        sm._runtimes[session.id] = AgentSessionRuntime(agent=agent)
        sm._persist(session)
        sm._runtimes.pop(session.id)

        loaded = sm._load_agent_messages(session.id)
        assert len(loaded) == 2
        assert loaded[0].tool_calls[0].name == "search"
        assert loaded[0].tool_calls[0].arguments == {"q": "test"}
        assert loaded[1].tool_results[0].tool_call_id == "tc1"
        assert loaded[1].tool_results[0].content == "found it"
        assert loaded[1].tool_results[0].is_error is False

    def test_error_tool_result_roundtrip(self, sm: SessionManager, storage_dir: Path):
        session = sm.create("ws1", session_type="mutbot.session.AgentSession")
        messages = [
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="tc1", name="run", arguments={})],
            ),
            Message(
                role="user",
                tool_results=[ToolResult(tool_call_id="tc1", content="Error!", is_error=True)],
            ),
        ]

        agent = MagicMock()
        agent.messages = messages
        sm._runtimes[session.id] = AgentSessionRuntime(agent=agent)
        sm._persist(session)
        sm._runtimes.pop(session.id)

        loaded = sm._load_agent_messages(session.id)
        assert loaded[1].tool_results[0].is_error is True


# ---------------------------------------------------------------------------
# 模拟 server 重启周期
# ---------------------------------------------------------------------------

class TestServerRestartCycle:
    """模拟 server 多次重启的场景：messages 不应丢失"""

    def test_restart_preserves_messages(self, storage_dir: Path):
        """重启周期：create → persist with messages → new SM load → persist again"""
        # 第一个 server 周期：创建 session 并保存 messages
        sm1 = SessionManager()
        session1 = sm1.create("ws1", session_type="mutbot.session.AgentSession")
        session_id = session1.id

        agent = MagicMock()
        agent.messages = _make_messages()
        sm1._runtimes[session_id] = AgentSessionRuntime(agent=agent)
        sm1._persist(session1)

        # 第二个 server 周期：load_from_disk → 无 runtime 下 persist（如 update title）
        sm2 = SessionManager()
        sm2.load_from_disk()
        session2 = sm2.get(session_id)
        assert session2 is not None

        session2.title = "New Title"
        sm2._persist(session2)

        data = _read_session_json(storage_dir, session_id)
        assert data["title"] == "New Title"
        assert len(data["messages"]) == 4, "重启后 messages 不应丢失"

    def test_multiple_restarts_preserve_messages(self, storage_dir: Path):
        """多次重启仍保留 messages"""
        sm1 = SessionManager()
        session = sm1.create("ws1", session_type="mutbot.session.AgentSession")
        sid = session.id

        agent = MagicMock()
        agent.messages = _make_messages()
        sm1._runtimes[sid] = AgentSessionRuntime(agent=agent)
        sm1._persist(session)

        # 模拟 3 次重启
        for i in range(3):
            sm_new = SessionManager()
            sm_new.load_from_disk()
            s = sm_new.get(sid)
            assert s is not None
            s.title = f"Restart {i}"
            sm_new._persist(s)

        data = _read_session_json(storage_dir, sid)
        assert data["title"] == "Restart 2"
        assert len(data["messages"]) == 4

    def test_restart_preserves_total_tokens(self, storage_dir: Path):
        """重启后 total_tokens 正确恢复"""
        sm1 = SessionManager()
        session = sm1.create("ws1", session_type="mutbot.session.AgentSession")
        assert isinstance(session, AgentSession)
        session.total_tokens = 42000
        sm1._persist(session)

        sm2 = SessionManager()
        sm2.load_from_disk()
        restored = sm2.get(session.id)
        assert isinstance(restored, AgentSession)
        assert restored.total_tokens == 42000


# ---------------------------------------------------------------------------
# stop() 场景
# ---------------------------------------------------------------------------

class TestStopPreservesMessages:
    """stop() 后 messages 应完整保存"""

    @pytest.mark.asyncio
    async def test_stop_with_runtime_preserves_messages(
        self, sm: SessionManager, storage_dir: Path,
    ):
        """stop() 在清除 runtime 前应先 persist messages"""
        session = sm.create("ws1", session_type="mutbot.session.AgentSession")

        # 注入 runtime
        agent = MagicMock()
        agent.messages = _make_messages()
        bridge = MagicMock()

        async def _noop_stop():
            pass

        bridge.stop = _noop_stop
        sm._runtimes[session.id] = AgentSessionRuntime(agent=agent, bridge=bridge)

        await sm.stop(session.id)

        # Runtime 应已清除
        assert sm.get_agent_runtime(session.id) is None

        # Messages 应保留在磁盘上
        data = _read_session_json(storage_dir, session.id)
        assert data["status"] == "ended"
        assert len(data["messages"]) == 4

    @pytest.mark.asyncio
    async def test_stop_without_runtime_preserves_existing_messages(
        self, sm: SessionManager, storage_dir: Path,
    ):
        """没有 runtime 的 session（如 ended 后重启），stop 不应丢 messages"""
        session = sm.create("ws1", session_type="mutbot.session.AgentSession")

        # 先写入 messages
        agent = MagicMock()
        agent.messages = _make_messages()
        sm._runtimes[session.id] = AgentSessionRuntime(agent=agent)
        sm._persist(session)
        sm._runtimes.pop(session.id)

        # 无 runtime 下 stop
        await sm.stop(session.id)

        data = _read_session_json(storage_dir, session.id)
        assert data["status"] == "ended"
        assert len(data["messages"]) == 4, "stop 不应覆盖已有 messages"


# 需要 asyncio 支持
import asyncio
