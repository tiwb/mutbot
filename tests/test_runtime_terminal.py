"""测试 ptyhost TerminalManager（PTY 进程池 + pyte 渲染引擎）

验证不需要真实 PTY 的核心逻辑：view 管理、query 接口、kill、resize。
通过直接注入 TerminalProcess 对象跳过 PTY spawn。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from mutbot.ptyhost._manager import (
    TerminalManager,
    TerminalProcess,
)


# ---------------------------------------------------------------------------
# Fixture: 构造不启动真实 PTY 的 TerminalManager
# ---------------------------------------------------------------------------

def _make_fake_term(term_id: str = "t1") -> TerminalProcess:
    """创建一个不关联真实进程的 TerminalProcess"""
    return TerminalProcess(id=term_id, rows=24, cols=80)


def _make_manager_with_term(
    term_id: str = "t1",
) -> tuple[TerminalManager, TerminalProcess]:
    """创建 TerminalManager 并注入一个假 terminal（跳过 PTY spawn）"""
    on_frame = MagicMock()
    on_exit = MagicMock()
    tm = TerminalManager(on_frame=on_frame, on_exit=on_exit)
    term = _make_fake_term(term_id)
    tm._terminals[term_id] = term
    tm._output_buffers[term_id] = bytearray()
    tm._render_pending[term_id] = False
    return tm, term


# ---------------------------------------------------------------------------
# View 管理
# ---------------------------------------------------------------------------

class TestView:
    """view 创建和销毁"""

    def test_create_view(self):
        tm, term = _make_manager_with_term("t1")
        # 需要 pyte screen 才能创建 view
        from mutbot.ptyhost._screen import _SafeHistoryScreen
        import pyte, codecs
        term.screen = _SafeHistoryScreen(80, 24, history=50000, ratio=0.001)
        term.stream = pyte.Stream(term.screen)
        term.decoder = codecs.getincrementaldecoder("utf-8")("replace")

        view_id = tm.create_view("t1")
        assert view_id is not None
        assert view_id in tm._views
        assert view_id in term.views

    def test_create_view_unknown_term(self):
        on_frame = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_frame=on_frame, on_exit=on_exit)
        assert tm.create_view("nonexistent") is None

    def test_destroy_view(self):
        tm, term = _make_manager_with_term("t1")
        from mutbot.ptyhost._screen import _SafeHistoryScreen
        import pyte, codecs
        term.screen = _SafeHistoryScreen(80, 24, history=50000, ratio=0.001)
        term.stream = pyte.Stream(term.screen)
        term.decoder = codecs.getincrementaldecoder("utf-8")("replace")

        view_id = tm.create_view("t1")
        assert view_id is not None
        tm.destroy_view(view_id)
        assert view_id not in tm._views
        assert view_id not in term.views


# ---------------------------------------------------------------------------
# Query 接口
# ---------------------------------------------------------------------------

class TestQuery:
    """has / status / list_all / count"""

    def test_has_returns_true(self):
        tm, _ = _make_manager_with_term("t1")
        assert tm.has("t1") is True

    def test_has_returns_false(self):
        on_frame = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_frame=on_frame, on_exit=on_exit)
        assert tm.has("nonexistent") is False

    def test_status_returns_info(self):
        tm, term = _make_manager_with_term("t1")
        s = tm.status("t1")
        assert s is not None
        assert s["alive"] is True
        assert s["rows"] == 24
        assert s["cols"] == 80

    def test_status_returns_none_for_unknown(self):
        on_frame = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_frame=on_frame, on_exit=on_exit)
        assert tm.status("nonexistent") is None

    def test_list_all(self):
        tm, _ = _make_manager_with_term("t1")
        t2 = _make_fake_term("t2")
        tm._terminals["t2"] = t2
        result = tm.list_all()
        assert len(result) == 2
        ids = {r["term_id"] for r in result}
        assert ids == {"t1", "t2"}

    def test_count(self):
        tm, _ = _make_manager_with_term("t1")
        assert tm.count == 1
        t2 = _make_fake_term("t2")
        tm._terminals["t2"] = t2
        assert tm.count == 2


# ---------------------------------------------------------------------------
# Kill
# ---------------------------------------------------------------------------

class TestKill:
    """kill 清理 terminal"""

    def test_kill_removes_terminal(self):
        tm, _ = _make_manager_with_term("t1")
        tm.kill("t1")
        assert tm.has("t1") is False

    def test_kill_nonexistent_no_error(self):
        on_frame = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_frame=on_frame, on_exit=on_exit)
        tm.kill("nonexistent")  # 不应抛出异常

    def test_kill_all(self):
        tm, _ = _make_manager_with_term("t1")
        tm._terminals["t2"] = _make_fake_term("t2")
        tm._output_buffers["t2"] = bytearray()
        tm._render_pending["t2"] = False
        tm.kill_all()
        assert tm.count == 0

    def test_kill_cleans_views(self):
        tm, term = _make_manager_with_term("t1")
        from mutbot.ptyhost._screen import _SafeHistoryScreen
        import pyte, codecs
        term.screen = _SafeHistoryScreen(80, 24, history=50000, ratio=0.001)
        term.stream = pyte.Stream(term.screen)
        term.decoder = codecs.getincrementaldecoder("utf-8")("replace")

        view_id = tm.create_view("t1")
        assert view_id is not None
        tm.kill("t1")
        assert view_id not in tm._views


# ---------------------------------------------------------------------------
# Resize
# ---------------------------------------------------------------------------

class TestResize:
    """resize 更新终端尺寸 + pyte screen"""

    def test_resize_updates_dimensions(self):
        tm, term = _make_manager_with_term("t1")
        from mutbot.ptyhost._screen import _SafeHistoryScreen
        import pyte, codecs
        term.screen = _SafeHistoryScreen(80, 24, history=50000, ratio=0.001)
        term.stream = pyte.Stream(term.screen)
        term.decoder = codecs.getincrementaldecoder("utf-8")("replace")

        result = tm.resize("t1", 40, 120)
        assert result == (40, 120)
        assert term.rows == 40
        assert term.cols == 120
        # pyte screen 也应该被 resize
        assert term.screen.lines == 40
        assert term.screen.columns == 120

    def test_resize_unknown_returns_none(self):
        on_frame = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_frame=on_frame, on_exit=on_exit)
        assert tm.resize("nonexistent", 40, 120) is None
