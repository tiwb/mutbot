"""Setup 向导集成测试

涵盖：
- ConnectionManager pending events 队列与 flush
- AgentBridge.send_message hidden 不广播
- _ensure_setup_session 创建/恢复逻辑
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# ConnectionManager pending events
# ---------------------------------------------------------------------------

class TestConnectionManagerPendingEvents:
    """测试 ConnectionManager 的 pending events 队列机制。"""

    def _make_manager(self):
        from mutbot.web.connection import ConnectionManager
        return ConnectionManager()

    def test_queue_event_stores(self):
        """无连接时 queue_event 存入 pending。"""
        cm = self._make_manager()
        cm.queue_event("ws1", "open_session", {"session_id": "s1"})
        assert "ws1" in cm._pending_events
        assert len(cm._pending_events["ws1"]) == 1
        msg = cm._pending_events["ws1"][0]
        assert msg["type"] == "event"
        assert msg["event"] == "open_session"
        assert msg["data"]["session_id"] == "s1"

    def test_queue_multiple_events(self):
        """多个事件入队。"""
        cm = self._make_manager()
        cm.queue_event("ws1", "event_a", {"x": 1})
        cm.queue_event("ws1", "event_b", {"y": 2})
        assert len(cm._pending_events["ws1"]) == 2

    @pytest.mark.asyncio
    async def test_flush_on_connect(self):
        """连接时 flush pending events 给新客户端。"""
        cm = self._make_manager()
        cm.queue_event("ws1", "open_session", {"session_id": "s1"})

        # 模拟 WebSocket
        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()

        await cm.connect("ws1", ws)

        ws.accept.assert_called_once()
        ws.send_json.assert_called_once()
        sent = ws.send_json.call_args[0][0]
        assert sent["event"] == "open_session"
        # flush 后 pending 清空
        assert "ws1" not in cm._pending_events

    @pytest.mark.asyncio
    async def test_no_pending_no_flush(self):
        """无 pending 时连接不 flush。"""
        cm = self._make_manager()
        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()

        await cm.connect("ws1", ws)

        ws.accept.assert_called_once()
        ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# AgentBridge hidden message
# ---------------------------------------------------------------------------

class TestAgentBridgeHidden:
    """测试 AgentBridge.send_message 的 hidden 标记行为。"""

    def _make_bridge(self, loop):
        from mutbot.web.agent_bridge import AgentBridge

        agent = MagicMock()
        broadcast_calls = []

        async def mock_broadcast(session_id, data):
            broadcast_calls.append(data)

        bridge = AgentBridge(
            session_id="test-session",
            agent=agent,
            loop=loop,
            broadcast_fn=mock_broadcast,
        )
        return bridge, broadcast_calls

    @pytest.mark.asyncio
    async def test_normal_message_broadcasts(self):
        """普通消息广播 user_message 和 thinking 状态。"""
        loop = asyncio.get_running_loop()
        bridge, calls = self._make_bridge(loop)
        bridge.send_message("hello", data={})

        # 入队了消息
        assert not bridge._input_queue.empty()
        # 让 ensure_future 的协程执行
        await asyncio.sleep(0.01)
        # 应广播 user_message 和 thinking
        types = [c.get("type") or c.get("status") for c in calls]
        assert any(c.get("type") == "user_message" for c in calls)
        assert any(c.get("status") == "thinking" for c in calls)

    @pytest.mark.asyncio
    async def test_hidden_message_no_broadcast(self):
        """hidden 消息不广播 user_message，不推送 thinking 状态。"""
        loop = asyncio.get_running_loop()
        bridge, calls = self._make_bridge(loop)
        bridge.send_message("__setup__", data={"hidden": True})

        # 入队了消息
        assert not bridge._input_queue.empty()
        # 让 ensure_future 协程有机会执行（如果有的话）
        await asyncio.sleep(0.01)
        # 不应有任何广播
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_hidden_message_enters_queue(self):
        """hidden 消息正常进入 input queue。"""
        loop = asyncio.get_running_loop()
        bridge, _ = self._make_bridge(loop)
        bridge.send_message("__setup__", data={"hidden": True})

        item = bridge._input_queue.get_nowait()
        assert item.type == "user_message"
        assert item.text == "__setup__"
        assert item.data.get("hidden") is True


# ---------------------------------------------------------------------------
# _ensure_setup_session
# ---------------------------------------------------------------------------

class FakeSession:
    def __init__(self, id, type, status="active", config=None):
        self.id = id
        self.type = type
        self.status = status
        self.config = config or {}


class FakeSessionManager:
    def __init__(self):
        self.sessions: list[FakeSession] = []
        self.created: list[FakeSession] = []
        self.persisted: list[FakeSession] = []

    def list_by_workspace(self, ws_id):
        return self.sessions

    def create(self, ws_id, session_type="", config=None):
        s = FakeSession(
            id=f"s_{len(self.created) + 1}",
            type=session_type,
            config=config or {},
        )
        self.created.append(s)
        self.sessions.append(s)
        return s

    def _persist(self, session):
        self.persisted.append(session)


class FakeWorkspace:
    def __init__(self, id="ws1"):
        self.id = id
        self.sessions: list[str] = []


class FakeWorkspaceManager:
    def __init__(self):
        self.updated: list = []

    def update(self, ws):
        self.updated.append(ws)


class TestEnsureSetupSession:
    """测试 _ensure_setup_session 逻辑。"""

    def _call(self, ws, sm, wm):
        """调用 _ensure_setup_session（需要 mock workspace_connection_manager）。"""
        from unittest.mock import patch

        mock_cm = MagicMock()
        with patch(
            "mutbot.web.routes.workspace_connection_manager", mock_cm,
        ):
            from mutbot.web.server import _ensure_setup_session
            _ensure_setup_session(ws, sm, wm)
        return mock_cm

    def test_first_start_creates_guide(self):
        """首次启动：创建 Guide session。"""
        ws = FakeWorkspace()
        sm = FakeSessionManager()
        wm = FakeWorkspaceManager()

        mock_cm = self._call(ws, sm, wm)

        assert len(sm.created) == 1
        guide = sm.created[0]
        assert guide.type == "mutbot.builtins.guide.GuideSession"
        assert guide.config.get("initial_message") == "__setup__"
        assert guide.id in ws.sessions
        assert len(wm.updated) == 1
        # 入队了 open_session 事件
        mock_cm.queue_event.assert_called_once()
        call_args = mock_cm.queue_event.call_args
        assert call_args[0][1] == "open_session"
        assert call_args[0][2]["session_id"] == guide.id

    def test_restart_reinjects_initial_message(self):
        """重启恢复：已有 Guide session，重新注入 initial_message。"""
        ws = FakeWorkspace()
        existing_guide = FakeSession(
            id="s_existing",
            type="mutbot.builtins.guide.GuideSession",
            status="active",
            config={},  # initial_message 已消费
        )
        sm = FakeSessionManager()
        sm.sessions = [existing_guide]
        wm = FakeWorkspaceManager()

        mock_cm = self._call(ws, sm, wm)

        # 不应创建新 session
        assert len(sm.created) == 0
        # 应重新注入 initial_message
        assert existing_guide.config.get("initial_message") == "__setup__"
        assert existing_guide in sm.persisted
        # 入队了 open_session
        mock_cm.queue_event.assert_called_once()

    def test_skip_if_initial_message_exists(self):
        """Guide session 已有 initial_message 时不重复注入。"""
        ws = FakeWorkspace()
        existing_guide = FakeSession(
            id="s_existing",
            type="mutbot.builtins.guide.GuideSession",
            status="active",
            config={"initial_message": "__setup__"},
        )
        sm = FakeSessionManager()
        sm.sessions = [existing_guide]
        wm = FakeWorkspaceManager()

        mock_cm = self._call(ws, sm, wm)

        assert len(sm.created) == 0
        assert len(sm.persisted) == 0
        # 仍然入队 open_session（让前端打开 tab）
        mock_cm.queue_event.assert_called_once()
