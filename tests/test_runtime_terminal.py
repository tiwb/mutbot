"""测试 ptyhost TerminalManager（PTY 进程池）

验证不需要真实 PTY 的核心逻辑：scrollback 管理、query 接口、kill。
通过直接注入 TerminalProcess 对象跳过 PTY spawn。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mutbot.ptyhost._manager import (
    TerminalManager,
    TerminalProcess,
    SCROLLBACK_MAX,
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
    on_output = MagicMock()
    on_exit = MagicMock()
    tm = TerminalManager(on_output=on_output, on_exit=on_exit)
    term = _make_fake_term(term_id)
    tm._terminals[term_id] = term
    return tm, term


# ---------------------------------------------------------------------------
# Scrollback 缓冲
# ---------------------------------------------------------------------------

class TestScrollback:
    """scrollback 缓冲区管理"""

    def test_scrollback_accumulates(self):
        tm, term = _make_manager_with_term("t1")
        tm._on_pty_output(term, b"hello")
        tm._on_pty_output(term, b" world")
        assert tm.get_scrollback("t1") == b"hello world"

    def test_scrollback_trims_to_max(self):
        tm, term = _make_manager_with_term("t1")
        big_data = b"x" * (SCROLLBACK_MAX + 100)
        tm._on_pty_output(term, big_data)
        sb = tm.get_scrollback("t1")
        assert len(sb) == SCROLLBACK_MAX
        assert sb == big_data[-SCROLLBACK_MAX:]

    def test_scrollback_empty_for_unknown(self):
        on_output = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_output=on_output, on_exit=on_exit)
        assert tm.get_scrollback("nonexistent") == b""

    def test_output_callback_called(self):
        on_output = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_output=on_output, on_exit=on_exit)
        term = _make_fake_term("t1")
        tm._terminals["t1"] = term
        tm._on_pty_output(term, b"hello")
        on_output.assert_called_once_with("t1", b"hello")


# ---------------------------------------------------------------------------
# Query 接口
# ---------------------------------------------------------------------------

class TestQuery:
    """has / status / list_all / count"""

    def test_has_returns_true(self):
        tm, _ = _make_manager_with_term("t1")
        assert tm.has("t1") is True

    def test_has_returns_false(self):
        on_output = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_output=on_output, on_exit=on_exit)
        assert tm.has("nonexistent") is False

    def test_status_returns_info(self):
        tm, term = _make_manager_with_term("t1")
        s = tm.status("t1")
        assert s is not None
        assert s["alive"] is True
        assert s["rows"] == 24
        assert s["cols"] == 80

    def test_status_returns_none_for_unknown(self):
        on_output = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_output=on_output, on_exit=on_exit)
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
        on_output = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_output=on_output, on_exit=on_exit)
        tm.kill("nonexistent")  # 不应抛出异常

    def test_kill_all(self):
        tm, _ = _make_manager_with_term("t1")
        tm._terminals["t2"] = _make_fake_term("t2")
        tm.kill_all()
        assert tm.count == 0


# ---------------------------------------------------------------------------
# Resize
# ---------------------------------------------------------------------------

class TestResize:
    """resize 更新终端尺寸"""

    def test_resize_updates_dimensions(self):
        tm, term = _make_manager_with_term("t1")
        result = tm.resize("t1", 40, 120)
        assert result == (40, 120)
        assert term.rows == 40
        assert term.cols == 120

    def test_resize_unknown_returns_none(self):
        on_output = MagicMock()
        on_exit = MagicMock()
        tm = TerminalManager(on_output=on_output, on_exit=on_exit)
        assert tm.resize("nonexistent", 40, 120) is None
