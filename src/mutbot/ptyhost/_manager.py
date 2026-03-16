"""PTY 进程池 — ptyhost 守护进程的核心。

管理 PTY 进程的创建、I/O、销毁。
内置 pyte HistoryScreen 做 ANSI 状态机，渲染 dirty diff 为 ANSI 帧推送。
TermView 提供独立滚动视口。
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import re
import select as _select
import signal
import struct
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import pyte

from mutbot.ptyhost._screen import _SafeHistoryScreen, TermView

logger = logging.getLogger("mutbot.ptyhost")

IS_WINDOWS = sys.platform == "win32"

# Strip OSC 0/1/2 (window/icon title) sequences
_OSC_TITLE_RE = re.compile(rb"\x1b\][012];[^\x07\x1b]*(?:\x07|\x1b\\)")

# 回调类型：(term_id, view_id, ansi_frame)
FrameCallback = Callable[[str, str, bytes], None]
ExitCallback = Callable[[str, int | None], None]  # (term_id, exit_code)


@dataclass
class TerminalProcess:
    id: str  # UUID hex (32 chars)
    rows: int
    cols: int
    process: Any = None
    reader_thread: threading.Thread | None = None
    alive: bool = True
    exit_code: int | None = field(default=None, repr=False)
    _fd: int | None = field(default=None, repr=False)
    # pyte 状态机
    screen: _SafeHistoryScreen | None = field(default=None, repr=False)
    stream: pyte.Stream | None = field(default=None, repr=False)
    decoder: codecs.IncrementalDecoder | None = field(default=None, repr=False)
    # view 管理
    views: dict[str, TermView] = field(default_factory=dict, repr=False)


class TerminalManager:
    """PTY 进程池 + pyte 渲染引擎。

    管理 PTY 进程的创建、I/O、销毁。
    内置 pyte 状态机，渲染 dirty diff 为 ANSI 帧推送。
    """

    def __init__(
        self,
        on_frame: FrameCallback,
        on_exit: ExitCallback,
    ) -> None:
        self._terminals: dict[str, TerminalProcess] = {}
        self._on_frame = on_frame
        self._on_exit = on_exit
        # view_id → TermView 的快速查找
        self._views: dict[str, TermView] = {}
        # 渲染管线状态
        self._output_buffers: dict[str, bytearray] = {}
        self._flush_handles: dict[str, asyncio.TimerHandle] = {}
        self._render_pending: dict[str, bool] = {}
        self._render_handle: asyncio.TimerHandle | None = None
        self._sync_timeout_handles: dict[str, asyncio.TimerHandle] = {}  # BSU 超时保护
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(self, rows: int, cols: int, cwd: str | None = None) -> str:
        """创建 PTY 进程 + pyte 状态机，返回 term_id (UUID hex)。"""
        term_id = uuid.uuid4().hex
        term = TerminalProcess(id=term_id, rows=rows, cols=cols)
        work_dir = cwd or os.getcwd()

        # 初始化 pyte 状态机
        screen = _SafeHistoryScreen(cols, rows, history=50000, ratio=0.001)
        stream = pyte.Stream(screen)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        term.screen = screen
        term.stream = stream
        term.decoder = decoder

        if IS_WINDOWS:
            self._spawn_windows(term, work_dir)
        else:
            self._spawn_unix(term, work_dir)

        self._terminals[term_id] = term
        self._output_buffers[term_id] = bytearray()
        self._render_pending[term_id] = False
        self._start_reader(term)
        logger.info("Terminal created: %s, cwd=%s", term_id, work_dir)
        return term_id

    def _spawn_windows(self, term: TerminalProcess, cwd: str) -> None:
        from winpty import PtyProcess  # type: ignore[import-untyped]

        shell = os.environ.get("COMSPEC", "cmd.exe")
        proc = PtyProcess.spawn(
            shell,
            dimensions=(term.rows, term.cols),
            cwd=cwd,
        )
        term.process = proc

    def _spawn_unix(self, term: TerminalProcess, cwd: str) -> None:
        import fcntl
        import pty
        import subprocess
        import termios

        shell = os.environ.get("SHELL", "/bin/bash")
        master_fd, slave_fd = pty.openpty()

        winsize = struct.pack("HHHH", term.rows, term.cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}

        def _preexec() -> None:
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen(
            [shell],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            preexec_fn=_preexec,
            close_fds=True,
            env=env,
        )
        os.close(slave_fd)

        term.process = proc
        term._fd = master_fd

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _start_reader(self, term: TerminalProcess) -> None:
        if term.reader_thread is not None:
            return

        term_id = term.id

        def reader() -> None:
            try:
                if IS_WINDOWS:
                    proc = term.process
                    while term.alive:
                        try:
                            data = proc.read(65536)
                            if not data:
                                break
                            if isinstance(data, str):
                                data = data.encode("utf-8", errors="replace")
                            self._on_pty_output(term, data)
                        except EOFError:
                            break
                        except Exception:
                            if not term.alive:
                                break
                            logger.exception("Terminal reader error: %s", term_id)
                            break
                else:
                    fd = term._fd
                    while term.alive and fd is not None:
                        try:
                            rlist, _, _ = _select.select([fd], [], [], 1.0)
                            if not rlist:
                                fd = term._fd
                                continue
                            data = os.read(fd, 65536)
                            if not data:
                                break
                            self._on_pty_output(term, data)
                        except OSError:
                            break
            finally:
                term.alive = False
                exit_code = None
                try:
                    if IS_WINDOWS:
                        proc = term.process
                        if proc:
                            exit_code = getattr(proc, "exitstatus", None)
                    else:
                        proc = term.process
                        if proc:
                            proc.wait(timeout=1)
                            exit_code = proc.returncode
                except Exception:
                    pass
                term.exit_code = exit_code
                logger.info("Terminal stopped: %s (exit_code=%s)", term_id, exit_code)
                self._on_exit(term_id, exit_code)

        t = threading.Thread(target=reader, daemon=True, name=f"ptyhost-reader-{term_id}")
        term.reader_thread = t
        t.start()

    def _on_pty_output(self, term: TerminalProcess, data: bytes) -> None:
        data = _OSC_TITLE_RE.sub(b"", data)
        if not data:
            return
        # 投递到事件循环，在主线程中 feed pyte + 触发渲染
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._on_data_from_pty, term.id, data)

    # ------------------------------------------------------------------
    # 渲染管线（事件循环线程）
    # ------------------------------------------------------------------

    _FLUSH_INTERVAL = 0.016   # 16ms flush 缓冲（攒多个 chunk 后再 feed pyte）
    _FLUSH_MAX_BYTES = 32 * 1024  # 32KB 立即 flush
    _RENDER_INTERVAL = 0.016  # 16ms 帧间隔 (~60fps)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """由 PtyHostApp 在 startup 时调用，设置事件循环引用。"""
        self._loop = loop

    def _on_data_from_pty(self, term_id: str, data: bytes) -> None:
        """事件循环线程：将 PTY 数据放入缓冲区，延迟 feed。"""
        term = self._terminals.get(term_id)
        if term is None or term.screen is None:
            return
        buf = self._output_buffers.get(term_id)
        if buf is None:
            return
        buf.extend(data)

        if len(buf) >= self._FLUSH_MAX_BYTES:
            # 超过上限，立即 flush
            self._cancel_flush_timer(term_id)
            self._flush_and_feed(term_id)
        elif term_id not in self._flush_handles:
            # 首次数据到达，启动 16ms 定时器
            loop = self._loop
            if loop is not None:
                handle = loop.call_later(
                    self._FLUSH_INTERVAL, self._flush_and_feed, term_id,
                )
                self._flush_handles[term_id] = handle

    def _cancel_flush_timer(self, term_id: str) -> None:
        handle = self._flush_handles.pop(term_id, None)
        if handle is not None:
            handle.cancel()

    def _flush_and_feed(self, term_id: str) -> None:
        """消费 output buffer，feed pyte stream，标记 render pending。"""
        self._flush_handles.pop(term_id, None)
        term = self._terminals.get(term_id)
        if term is None or term.screen is None or term.stream is None or term.decoder is None:
            return
        buf = self._output_buffers.get(term_id)
        if not buf:
            return
        data = bytes(buf)
        buf.clear()
        was_synchronized = term.screen.synchronized
        try:
            text = term.decoder.decode(data)
            if text:
                term.stream.feed(text)
        except RuntimeError:
            logger.warning("pyte stream corrupted for %s, rebuilding", term_id)
            screen = _SafeHistoryScreen(term.cols, term.rows, history=50000, ratio=0.001)
            stream = pyte.Stream(screen)
            term.screen = screen
            term.stream = stream
            term.decoder = codecs.getincrementaldecoder("utf-8")("replace")
            return
        # BSU 期间不调度渲染，等 ESU 到达后一次性渲染
        if term.screen.synchronized:
            # 超时保护：BSU 后 500ms 未收到 ESU，强制渲染（程序崩溃等）
            if term_id not in self._sync_timeout_handles and self._loop:
                self._sync_timeout_handles[term_id] = self._loop.call_later(
                    0.5, self._force_end_sync, term_id,
                )
            return
        # ESU 到达，取消超时保护
        handle = self._sync_timeout_handles.pop(term_id, None)
        if handle is not None:
            handle.cancel()
        # 标记需要渲染（仅 live view 需要）
        has_live = any(v.scroll_offset == 0 for v in term.views.values())
        if has_live and term.screen.dirty:
            self._render_pending[term_id] = True
            # ESU 刚结束（was_synchronized → not synchronized）：立即渲染，不等定时器
            if was_synchronized:
                self._do_render_term(term_id)
            else:
                self._schedule_render()

    def _schedule_render(self) -> None:
        """调度渲染定时器（如果尚未调度）。"""
        if self._render_handle is not None:
            return
        loop = self._loop
        if loop is None:
            return
        self._render_handle = loop.call_later(self._RENDER_INTERVAL, self._render_frame)

    def _render_frame(self) -> None:
        """渲染定时器回调：处理所有 pending 终端的 dirty diff。"""
        self._render_handle = None
        for term_id in list(self._render_pending):
            if not self._render_pending.get(term_id):
                continue
            term = self._terminals.get(term_id)
            if term is None or term.screen is None:
                self._render_pending[term_id] = False
                continue
            # BSU 期间跳过渲染，保留 pending 状态等 ESU
            if term.screen.synchronized:
                continue
            self._do_render_term(term_id)

    def _do_render_term(self, term_id: str) -> None:
        """立即渲染单个终端的 dirty diff 并推送。"""
        from mutbot.ptyhost.ansi_render import render_dirty
        self._render_pending[term_id] = False
        term = self._terminals.get(term_id)
        if term is None or term.screen is None:
            return
        # 消费剩余 buffer
        self._flush_and_feed(term_id)
        frame = render_dirty(term.screen)
        if not frame:
            return
        # 推送给所有 live view
        for view in term.views.values():
            if view.scroll_offset == 0:
                self._on_frame(term_id, view.id, frame)

    def _force_end_sync(self, term_id: str) -> None:
        """BSU 超时保护：强制结束 synchronized 状态并渲染。"""
        self._sync_timeout_handles.pop(term_id, None)
        term = self._terminals.get(term_id)
        if term is None or term.screen is None:
            return
        if term.screen.synchronized:
            logger.debug("BSU timeout for %s, forcing render", term_id[:8])
            term.screen.synchronized = False
        if term.screen.dirty:
            self._render_pending[term_id] = True
            self._do_render_term(term_id)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def write(self, term_id: str, data: bytes) -> None:
        term = self._terminals.get(term_id)
        if term is None or not term.alive:
            return

        if IS_WINDOWS:
            try:
                d = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
                term.process.write(d)
            except Exception:
                logger.exception("Terminal write error: %s", term_id)
        else:
            fd = term._fd
            if fd is not None:
                try:
                    os.write(fd, data)
                except OSError:
                    logger.exception("Terminal write error: %s", term_id)

    def resize(self, term_id: str, rows: int, cols: int) -> tuple[int, int] | None:
        term = self._terminals.get(term_id)
        if term is None or not term.alive:
            return None
        term.rows = rows
        term.cols = cols
        self._set_pty_size(term, rows, cols)
        # 同步 resize pyte Screen
        if term.screen is not None:
            term.screen.resize(rows, cols)
            # resize 后全屏 dirty
            term.screen.dirty.update(range(term.screen.lines))
            self._render_pending[term_id] = True
            self._schedule_render()
        return (rows, cols)

    def _set_pty_size(self, term: TerminalProcess, rows: int, cols: int) -> None:
        if IS_WINDOWS:
            try:
                term.process.setwinsize(rows, cols)
            except Exception:
                logger.debug("Terminal resize failed: %s", term.id)
        else:
            import fcntl
            import termios
            fd = term._fd
            if fd is not None:
                try:
                    winsize = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                except Exception:
                    logger.debug("Terminal resize failed: %s", term.id)

    # ------------------------------------------------------------------
    # View 管理
    # ------------------------------------------------------------------

    def create_view(self, term_id: str) -> str | None:
        """创建 view，返回 view_id。"""
        term = self._terminals.get(term_id)
        if term is None:
            return None
        view_id = uuid.uuid4().hex[:16]  # 8 bytes hex
        view = TermView(id=view_id, term_id=term_id)
        term.views[view_id] = view
        self._views[view_id] = view
        return view_id

    def destroy_view(self, view_id: str) -> None:
        """销毁 view。"""
        view = self._views.pop(view_id, None)
        if view is None:
            return
        term = self._terminals.get(view.term_id)
        if term is not None:
            term.views.pop(view_id, None)

    def get_snapshot(self, view_id: str) -> bytes:
        """返回 view 当前可见内容的 ANSI 帧。"""
        from mutbot.ptyhost.ansi_render import render_full, render_lines
        view = self._views.get(view_id)
        if view is None:
            return b""
        term = self._terminals.get(view.term_id)
        if term is None or term.screen is None:
            return b""
        screen = term.screen
        if view.scroll_offset == 0:
            return render_full(screen)
        else:
            return self._render_scrolled_view(screen, view.scroll_offset)

    def scroll_view(self, view_id: str, lines: int) -> None:
        """滚动 view。lines>0 向上（看历史），lines<0 向下。"""
        view = self._views.get(view_id)
        if view is None:
            return
        term = self._terminals.get(view.term_id)
        if term is None or term.screen is None:
            return
        screen = term.screen
        max_offset = len(screen.history.top)
        new_offset = max(0, min(view.scroll_offset + lines, max_offset))
        if new_offset == view.scroll_offset:
            return
        view.scroll_offset = new_offset
        if new_offset == 0:
            # 回到 live，推送全屏快照
            from mutbot.ptyhost.ansi_render import render_full
            frame = render_full(screen)
        else:
            frame = self._render_scrolled_view(screen, new_offset)
        if frame:
            self._on_frame(view.term_id, view.id, frame)

    def scroll_view_to(self, view_id: str, offset: int) -> None:
        """滚动 view 到绝对偏移。offset=0 为 live（最底部），>0 为从底部往上行数。"""
        view = self._views.get(view_id)
        if view is None:
            return
        term = self._terminals.get(view.term_id)
        if term is None or term.screen is None:
            return
        screen = term.screen
        max_offset = len(screen.history.top)
        new_offset = max(0, min(offset, max_offset))
        if new_offset == view.scroll_offset:
            return
        view.scroll_offset = new_offset
        if new_offset == 0:
            from mutbot.ptyhost.ansi_render import render_full
            frame = render_full(screen)
        else:
            frame = self._render_scrolled_view(screen, new_offset)
        if frame:
            self._on_frame(view.term_id, view.id, frame)

    def scroll_view_to_bottom(self, view_id: str) -> None:
        """view 回到 live。"""
        view = self._views.get(view_id)
        if view is None:
            return
        if view.scroll_offset == 0:
            return
        view.scroll_offset = 0
        term = self._terminals.get(view.term_id)
        if term is None or term.screen is None:
            return
        from mutbot.ptyhost.ansi_render import render_full
        frame = render_full(term.screen)
        if frame:
            self._on_frame(view.term_id, view.id, frame)

    def _render_scrolled_view(self, screen: _SafeHistoryScreen, offset: int) -> bytes:
        """渲染 scrolled view 的可见内容。"""
        from mutbot.ptyhost.ansi_render import render_lines
        history_top = list(screen.history.top)
        buffer_lines = [screen.buffer[row] for row in range(screen.lines)]
        all_lines = history_top + buffer_lines
        total = len(all_lines)
        visible = screen.lines
        # offset 是从底部往上偏移
        end = total - offset
        start = max(0, end - visible)
        end = start + visible  # 确保刚好 visible 行
        lines_slice = all_lines[start:end]
        # 如果不够 visible 行，用空行补齐
        while len(lines_slice) < visible:
            lines_slice.append({})
        return render_lines(lines_slice, screen.columns, screen.default_char)

    def get_scroll_state(self, view_id: str) -> dict[str, int] | None:
        """返回滚动状态用于 scrollbar。"""
        view = self._views.get(view_id)
        if view is None:
            return None
        term = self._terminals.get(view.term_id)
        if term is None or term.screen is None:
            return None
        screen = term.screen
        return {
            "offset": view.scroll_offset,
            "total": len(screen.history.top) + screen.lines,
            "visible": screen.lines,
        }

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def status(self, term_id: str) -> dict[str, Any] | None:
        term = self._terminals.get(term_id)
        if term is None:
            return None
        return {
            "alive": term.alive,
            "exit_code": term.exit_code,
            "rows": term.rows,
            "cols": term.cols,
        }

    def list_all(self) -> list[dict[str, Any]]:
        return [
            {"term_id": t.id, "alive": t.alive}
            for t in self._terminals.values()
        ]

    def has(self, term_id: str) -> bool:
        return term_id in self._terminals

    @property
    def count(self) -> int:
        return len(self._terminals)

    # ------------------------------------------------------------------
    # Destroy
    # ------------------------------------------------------------------

    def kill(self, term_id: str) -> None:
        term = self._terminals.pop(term_id, None)
        if term is None:
            return

        term.alive = False
        logger.info("Killing terminal: %s", term_id)

        # 清理 pyte + views + 渲染状态
        for view_id in list(term.views):
            self._views.pop(view_id, None)
        term.views.clear()
        term.screen = None
        term.stream = None
        term.decoder = None
        self._cancel_flush_timer(term_id)
        self._output_buffers.pop(term_id, None)
        self._render_pending.pop(term_id, None)

        if IS_WINDOWS:
            try:
                if term.process and term.process.isalive():
                    term.process.terminate()
            except Exception:
                pass
        else:
            try:
                if term.process:
                    pgid = os.getpgid(term.process.pid)
                    os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    if term.process:
                        term.process.kill()
                except Exception:
                    pass
            try:
                if term._fd is not None:
                    os.close(term._fd)
                    term._fd = None
            except OSError:
                pass

    def kill_all(self) -> None:
        for term_id in list(self._terminals):
            self.kill(term_id)
