"""测试 TerminalManager 回调抽象（Phase 1 terminal 迁移）

验证 TerminalManager 使用 OutputCallback 回调而非直接依赖 WebSocket。
不启动真实 PTY，仅测试连接管理和输出广播逻辑。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

from mutbot.runtime.terminal import (
    TerminalManager,
    TerminalSession,
    OutputCallback,
    SCROLLBACK_MAX,
)


# ---------------------------------------------------------------------------
# Fixture: 构造一个不启动真实 PTY 的 TerminalManager
# ---------------------------------------------------------------------------

def _make_fake_session(term_id: str = "t1", workspace_id: str = "ws1") -> TerminalSession:
    """创建一个不关联真实进程的 TerminalSession"""
    return TerminalSession(
        id=term_id,
        workspace_id=workspace_id,
        rows=24,
        cols=80,
    )


def _make_manager_with_session(
    term_id: str = "t1",
) -> tuple[TerminalManager, TerminalSession]:
    """创建 TerminalManager 并注入一个假 session（跳过 PTY spawn）"""
    tm = TerminalManager()
    session = _make_fake_session(term_id)
    tm._sessions[term_id] = session
    return tm, session


# ---------------------------------------------------------------------------
# 连接管理
# ---------------------------------------------------------------------------

class TestConnectionManagement:
    """attach / detach / connection_count 使用回调接口"""

    def test_attach_registers_callback(self):
        tm, _ = _make_manager_with_session("t1")
        loop = asyncio.new_event_loop()
        callback = AsyncMock()

        tm.attach("t1", "client_a", callback, loop)
        assert tm.connection_count("t1") == 1
        loop.close()

    def test_attach_multiple_clients(self):
        tm, _ = _make_manager_with_session("t1")
        loop = asyncio.new_event_loop()

        tm.attach("t1", "client_a", AsyncMock(), loop)
        tm.attach("t1", "client_b", AsyncMock(), loop)
        assert tm.connection_count("t1") == 2
        loop.close()

    def test_detach_removes_callback(self):
        tm, _ = _make_manager_with_session("t1")
        loop = asyncio.new_event_loop()

        tm.attach("t1", "client_a", AsyncMock(), loop)
        tm.detach("t1", "client_a")
        assert tm.connection_count("t1") == 0
        loop.close()

    def test_detach_unknown_client_no_error(self):
        tm, _ = _make_manager_with_session("t1")
        # 不应抛出异常
        tm.detach("t1", "nonexistent")

    def test_detach_one_of_many(self):
        tm, _ = _make_manager_with_session("t1")
        loop = asyncio.new_event_loop()

        tm.attach("t1", "a", AsyncMock(), loop)
        tm.attach("t1", "b", AsyncMock(), loop)
        tm.detach("t1", "a")
        assert tm.connection_count("t1") == 1
        loop.close()

    def test_connection_count_no_session(self):
        tm = TerminalManager()
        assert tm.connection_count("nonexistent") == 0


# ---------------------------------------------------------------------------
# 输出广播
# ---------------------------------------------------------------------------

class TestOutputBroadcast:
    """_on_pty_output 通过回调广播到所有客户端"""

    def test_broadcast_calls_all_callbacks(self):
        tm, session = _make_manager_with_session("t1")
        loop = asyncio.new_event_loop()

        cb_a = AsyncMock()
        cb_b = AsyncMock()
        tm.attach("t1", "a", cb_a, loop)
        tm.attach("t1", "b", cb_b, loop)

        # 模拟 PTY 输出
        tm._on_pty_output(session, b"hello")

        # 回调应通过 run_coroutine_threadsafe 调度
        # 运行 loop 来执行调度的 coroutine
        loop.run_until_complete(asyncio.sleep(0.05))

        cb_a.assert_called_once_with(b"\x01hello")
        cb_b.assert_called_once_with(b"\x01hello")
        loop.close()

    def test_broadcast_no_clients_no_error(self):
        tm, session = _make_manager_with_session("t1")
        # 没有客户端时不应抛出异常
        tm._on_pty_output(session, b"hello")

    def test_broadcast_removes_dead_callback(self):
        tm, session = _make_manager_with_session("t1")
        loop = asyncio.new_event_loop()

        def bad_callback(data):
            raise RuntimeError("dead")

        tm.attach("t1", "dead_client", bad_callback, loop)
        tm._on_pty_output(session, b"hello")
        # dead callback 应被移除
        assert tm.connection_count("t1") == 0
        loop.close()


# ---------------------------------------------------------------------------
# Scrollback 缓冲
# ---------------------------------------------------------------------------

class TestScrollback:
    """scrollback 缓冲区管理"""

    def test_scrollback_accumulates(self):
        tm, session = _make_manager_with_session("t1")
        tm._on_pty_output(session, b"hello")
        tm._on_pty_output(session, b" world")
        assert tm.get_scrollback("t1") == b"hello world"

    def test_scrollback_trims_to_max(self):
        tm, session = _make_manager_with_session("t1")
        # 写入超过 SCROLLBACK_MAX 的数据
        big_data = b"x" * (SCROLLBACK_MAX + 100)
        tm._on_pty_output(session, big_data)
        sb = tm.get_scrollback("t1")
        assert len(sb) == SCROLLBACK_MAX
        # 应保留尾部数据
        assert sb == big_data[-SCROLLBACK_MAX:]

    def test_scrollback_empty_for_unknown(self):
        tm = TerminalManager()
        assert tm.get_scrollback("nonexistent") == b""


# ---------------------------------------------------------------------------
# 退出信号
# ---------------------------------------------------------------------------

class TestExitPayload:
    """_make_exit_payload 构造退出信号"""

    def test_exit_payload_no_code(self):
        tm = TerminalManager()
        payload = tm._make_exit_payload()
        assert payload == b"\x04"

    def test_exit_payload_with_code(self):
        tm = TerminalManager()
        payload = tm._make_exit_payload(0)
        assert payload[0:1] == b"\x04"
        assert len(payload) == 5  # 1 byte type + 4 bytes int

    def test_exit_payload_negative_code(self):
        tm = TerminalManager()
        payload = tm._make_exit_payload(-1)
        assert payload[0:1] == b"\x04"
        assert len(payload) == 5


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------

class TestQuery:
    """has / get / list_by_workspace"""

    def test_has_returns_true(self):
        tm, _ = _make_manager_with_session("t1")
        assert tm.has("t1") is True

    def test_has_returns_false(self):
        tm = TerminalManager()
        assert tm.has("nonexistent") is False

    def test_get_returns_session(self):
        tm, session = _make_manager_with_session("t1")
        assert tm.get("t1") is session

    def test_get_returns_none(self):
        tm = TerminalManager()
        assert tm.get("nonexistent") is None

    def test_list_by_workspace(self):
        tm = TerminalManager()
        s1 = _make_fake_session("t1", "ws1")
        s2 = _make_fake_session("t2", "ws1")
        s3 = _make_fake_session("t3", "ws2")
        tm._sessions["t1"] = s1
        tm._sessions["t2"] = s2
        tm._sessions["t3"] = s3
        result = tm.list_by_workspace("ws1")
        assert len(result) == 2
        assert all(s.workspace_id == "ws1" for s in result)


# ---------------------------------------------------------------------------
# Kill
# ---------------------------------------------------------------------------

class TestKill:
    """kill 清理 session 和连接"""

    def test_kill_removes_session(self):
        tm, _ = _make_manager_with_session("t1")
        tm.kill("t1")
        assert tm.has("t1") is False

    def test_kill_clears_connections(self):
        tm, _ = _make_manager_with_session("t1")
        loop = asyncio.new_event_loop()
        tm.attach("t1", "client_a", AsyncMock(), loop)
        tm.kill("t1")
        assert tm.connection_count("t1") == 0
        loop.close()

    def test_kill_nonexistent_no_error(self):
        tm = TerminalManager()
        tm.kill("nonexistent")  # 不应抛出异常

    def test_kill_all(self):
        tm = TerminalManager()
        tm._sessions["t1"] = _make_fake_session("t1")
        tm._sessions["t2"] = _make_fake_session("t2")
        tm.kill_all()
        assert len(tm._sessions) == 0
