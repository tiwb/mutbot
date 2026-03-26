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

# ---------------------------------------------------------------------------
# pyte CSI 兼容层 — 绕过 pyte 0.8.2 对现代终端序列的解析缺陷
# ---------------------------------------------------------------------------
# pyte 0.8.2 是 PyPI 最新版，项目维护不活跃（已知 PR 挂 1.5 年未合并）。
# 长远看需要完全重写终端状态机（pyte 为 LGPL v3，vendor 有许可约束）。
# 本次采用最小侵入的预处理方案绕过以下两个缺陷：
#
# 缺陷 1：CSI `>` 前缀被静默忽略
#   pyte 的 _parser_fsm 遇到 `>` 时执行 `pass`（本意跳过 Secondary DA），
#   但后续参数仍被正常解析并分派。导致 CSI > Ps m（如 modifyOtherKeys）
#   被误读为 SGR Ps m。例如 Claude Code 启动时发送 CSI > 4 ; 2 m
#   （启用 modifyOtherKeys），pyte 误读为 SGR 4（underline ON），
#   cursor.attrs.underscore 被卡死为 True，cls 清屏也无法恢复。
#
# 缺陷 2：冒号子参数（colon sub-parameters）不支持
#   CSI 解析器不识别 `:` 作为 ITU T.416 子参数分隔符，遇到 `:` 时
#   直接中断 CSI 解析。导致 \x1b[4:0m（underline OFF）无法被处理，
#   冒号后内容被当作纯文本输出（\x1b[4:3m → "3m" 显示为可见文字）。
#   参考：https://github.com/selectel/pyte/issues/179
#         https://github.com/selectel/pyte/pull/180
# ---------------------------------------------------------------------------

# 匹配需要剥离的 CSI > 私有序列（pyte 会误解析 > 后的参数）
# 覆盖 CSI > Ps ; Ps m/n/p 等所有 final characters
_CSI_GT_RE = re.compile(r"\x1b\[>[0-9;]*[a-zA-Z]")

# 匹配 SGR 序列：ESC[ + 数字/分号/冒号 + m
_SGR_COLON_RE = re.compile(r"\x1b\[([0-9:;]*)m")


def _normalize_sgr_group(group: str) -> str | None:
    """归一化单个 SGR 参数组（`;` 分隔的一段）中的冒号子参数。

    返回归一化后的参数字符串，或 None 表示该组应被删除。
    """
    parts = group.split(":")
    main = parts[0]
    subs = parts[1:]

    # 4:N — underline 样式
    if main == "4":
        if subs and subs[0] == "0":
            return "24"  # underline off
        return "4"       # underline on（pyte 不支持样式区分）

    # 38:2/48:2 — RGB 颜色（前景/背景）
    # 标准: 38:2:CS:R:G:B（含 colorspace），实际常见: 38:2:R:G:B（省略 CS）
    if main in ("38", "48") and len(subs) >= 1:
        if subs[0] == "2":
            # RGB — 按子参数个数判断是否含 colorspace ID
            rgb = subs[1:]  # 去掉 "2"
            if len(rgb) >= 4:
                # 有 CS: 38:2:CS:R:G:B → 取后 3 个
                r, g, b = rgb[1], rgb[2], rgb[3]
            elif len(rgb) == 3:
                # 无 CS: 38:2:R:G:B
                r, g, b = rgb[0], rgb[1], rgb[2]
            else:
                return main  # 格式异常，保留主参数
            return f"{main};2;{r};{g};{b}"
        elif subs[0] == "5" and len(subs) >= 2:
            # 256 色: 38:5:N → 38;5;N
            return f"{main};5;{subs[1]}"
        return main  # 未知子模式，保留主参数

    # 58:... — underline color，pyte 无对应属性，整组删除
    if main == "58":
        return None

    # 其他含冒号的参数：保留主参数，丢弃子参数
    return main


def _normalize_sgr_subparams(text: str) -> str:
    """预处理终端输出，绕过 pyte 对现代 CSI 序列的解析缺陷。

    在 pyte stream.feed() 之前调用，处理两类问题：
    1. 剥离 CSI > 私有序列（pyte 会忽略 > 但误解析后续参数）
    2. 归一化 SGR 冒号子参数（pyte 不支持 ITU T.416 子参数分隔符）
    """
    # 第一步：剥离 CSI > 序列（如 modifyOtherKeys: CSI > 4 ; 2 m）
    text = _CSI_GT_RE.sub("", text)

    # 第二步：归一化 SGR 冒号子参数
    def replace_sgr(m: re.Match) -> str:
        params_str = m.group(1)
        if ":" not in params_str:
            return m.group(0)  # 无冒号，原样返回

        groups = params_str.split(";")
        normalized: list[str] = []
        for g in groups:
            if ":" in g:
                result = _normalize_sgr_group(g)
                if result is not None:
                    normalized.append(result)
            else:
                normalized.append(g)

        return f"\x1b[{';'.join(normalized)}m"

    return _SGR_COLON_RE.sub(replace_sgr, text)

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
        self._flush_max_handles: dict[str, asyncio.TimerHandle] = {}  # 300ms 保底定时器
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

    _FLUSH_INTERVAL = 0.016   # 16ms 静默期（最后一次数据后等 16ms 再 flush）
    _FLUSH_MAX_DELAY = 0.3    # 300ms 保底上限（首次数据后最长等待时间）
    _RENDER_INTERVAL = 0.016  # 16ms 帧间隔 (~60fps)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """由 PtyHostApp 在 startup 时调用，设置事件循环引用。"""
        self._loop = loop

    def _on_data_from_pty(self, term_id: str, data: bytes) -> None:
        """事件循环线程：将 PTY 数据放入缓冲区，静默期 flush。

        策略：
        - 每次新数据到达，重置 16ms 静默期定时器
        - 数据停止 16ms 后 flush（认为一次输出结束）
        - 首次数据启动 300ms 保底定时器，防止持续输出时屏幕卡住
        """
        term = self._terminals.get(term_id)
        if term is None or term.screen is None:
            return
        buf = self._output_buffers.get(term_id)
        if buf is None:
            return
        buf.extend(data)

        # 每次新数据到达，重置静默期定时器
        self._cancel_flush_timer(term_id)
        loop = self._loop
        if loop is not None:
            handle = loop.call_later(
                self._FLUSH_INTERVAL, self._flush_and_feed, term_id,
            )
            self._flush_handles[term_id] = handle

            # 首次数据：启动 300ms 保底定时器
            if term_id not in self._flush_max_handles:
                max_handle = loop.call_later(
                    self._FLUSH_MAX_DELAY, self._flush_max_expired, term_id,
                )
                self._flush_max_handles[term_id] = max_handle

    def _flush_max_expired(self, term_id: str) -> None:
        """300ms 保底定时器到期：强制 flush，防止屏幕卡住。"""
        self._flush_max_handles.pop(term_id, None)
        buf = self._output_buffers.get(term_id)
        if buf:
            logger.warning(
                "Flush max delay: %s %dKB after %.0fms",
                term_id[:8], len(buf) // 1024, self._FLUSH_MAX_DELAY * 1000,
            )
            self._cancel_flush_timer(term_id)
            self._flush_and_feed(term_id)

    def _cancel_flush_timer(self, term_id: str) -> None:
        handle = self._flush_handles.pop(term_id, None)
        if handle is not None:
            handle.cancel()

    def _flush_and_feed(self, term_id: str) -> None:
        """消费 output buffer，feed pyte stream，标记 render pending。"""
        self._flush_handles.pop(term_id, None)
        # 清理保底定时器（buf 已被消费，后续新数据会重新启动）
        max_handle = self._flush_max_handles.pop(term_id, None)
        if max_handle is not None:
            max_handle.cancel()
        term = self._terminals.get(term_id)
        if term is None or term.screen is None or term.stream is None or term.decoder is None:
            return
        buf = self._output_buffers.get(term_id)
        if not buf:
            return
        data = bytes(buf)
        buf.clear()
        feed_size = len(data)
        was_synchronized = term.screen.synchronized
        prev_cx, prev_cy = term.screen.cursor.x, term.screen.cursor.y
        try:
            text = term.decoder.decode(data)
            if text:
                text = _normalize_sgr_subparams(text)
                term.stream.feed(text)
        except RuntimeError:
            logger.warning("pyte stream corrupted for %s, rebuilding", term_id)
            screen = _SafeHistoryScreen(term.cols, term.rows, history=50000, ratio=0.001)
            stream = pyte.Stream(screen)
            term.screen = screen
            term.stream = stream
            term.decoder = codecs.getincrementaldecoder("utf-8")("replace")
            return
        now_synchronized = term.screen.synchronized
        if feed_size > 32768:
            logger.info(
                "Large feed: %s %dKB (%d dirty lines, sync %s→%s)",
                term_id[:8], feed_size // 1024,
                len(term.screen.dirty),
                was_synchronized, now_synchronized,
            )
        # BSU/ESU 转换日志（不限大小，捕捉小 feed 的状态变化）
        if was_synchronized != now_synchronized:
            logger.info(
                "Sync change: %s %dB sync %s→%s (%d dirty)",
                term_id[:8], feed_size,
                was_synchronized, now_synchronized,
                len(term.screen.dirty),
            )
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
        elif has_live and not term.screen.dirty:
            # dirty 为空但光标移动了 → 发送轻量光标帧
            cur = term.screen.cursor
            if (cur.x, cur.y) != (prev_cx, prev_cy):
                self._emit_cursor_frame(term_id, term)

    def _emit_cursor_frame(self, term_id: str, term: "TerminalProcess") -> None:
        """生成并推送轻量光标帧（仅含光标定位，不含行内容）。"""
        screen = term.screen
        if screen is None:
            return
        cx, cy = screen.cursor.x, screen.cursor.y
        frame = f"\x1b[?25l\x1b[{cy + 1};{cx + 1}H\x1b[?25h".encode("utf-8")
        for view in term.views.values():
            if view.scroll_offset != 0:
                continue
            self._on_frame(term_id, view.id, frame)

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
        screen = term.screen
        # BSU 保护：_flush_and_feed 可能消费了含 BSU 的新数据，
        # 此时渲染会输出中间态帧导致闪烁。跳过渲染，等 ESU 到达后再渲染。
        if screen.synchronized:
            self._render_pending[term_id] = True
            logger.info(
                "Render deferred (BSU active after flush): %s (%d dirty lines)",
                term_id[:8], len(screen.dirty),
            )
            return
        dirty_count = len(screen.dirty)
        # 增量帧：给全屏 view (viewport_rows >= screen.lines) 使用
        frame = render_dirty(screen)
        frame_kb = len(frame) / 1024 if frame else 0
        if frame_kb > 4:
            logger.info(
                "Large frame: %s %.1fKB (%d dirty lines, %d views)",
                term_id[:8], frame_kb, dirty_count,
                sum(1 for v in term.views.values() if v.scroll_offset == 0),
            )
        # 推送给所有 live view
        for view in term.views.values():
            if view.scroll_offset != 0:
                continue
            if self._view_needs_viewport(view, screen):
                # viewport 模式：全量渲染视口范围
                vp_frame = self._render_viewport_frame(view, screen, 0)
                if vp_frame:
                    self._on_frame(term_id, view.id, vp_frame)
            elif frame:
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
        else:
            # dirty 为空但光标可能已移动（BSU 期间累积的光标变化），发送光标帧兜底
            self._emit_cursor_frame(term_id, term)

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

    def create_view(
        self, term_id: str, viewport_rows: int = 0, viewport_cols: int = 0,
    ) -> str | None:
        """创建 view，返回 view_id。"""
        term = self._terminals.get(term_id)
        if term is None:
            return None
        view_id = uuid.uuid4().hex[:8]  # 8 字符，匹配二进制帧头字段宽度
        view = TermView(
            id=view_id, term_id=term_id,
            viewport_rows=viewport_rows, viewport_cols=viewport_cols,
        )
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

    def set_viewport(self, view_id: str, rows: int, cols: int = 0) -> None:
        """设置 view 的 viewport 尺寸。0 表示使用终端原始尺寸。"""
        view = self._views.get(view_id)
        if view is None:
            return
        view.viewport_rows = max(0, rows)
        view.viewport_cols = max(0, cols)
        # 如果是 live view 且 viewport 改变，推送新帧
        if view.scroll_offset == 0:
            term = self._terminals.get(view.term_id)
            if term is None or term.screen is None:
                return
            if self._view_needs_viewport(view, term.screen):
                frame = self._render_viewport_frame(view, term.screen, 0)
            else:
                from mutbot.ptyhost.ansi_render import render_full
                frame = render_full(term.screen)
            if frame:
                self._on_frame(view.term_id, view.id, frame)

    def get_snapshot(self, view_id: str) -> bytes:
        """返回 view 当前可见内容的 ANSI 帧。"""
        from mutbot.ptyhost.ansi_render import render_full
        view = self._views.get(view_id)
        if view is None:
            return b""
        term = self._terminals.get(view.term_id)
        if term is None or term.screen is None:
            return b""
        screen = term.screen
        if view.scroll_offset == 0 and not self._view_needs_viewport(view, screen):
            return render_full(screen)
        else:
            return self._render_viewport_frame(view, screen, view.scroll_offset)

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
        # viewport 模式：屏幕内也可滚动
        vp = view.viewport_rows
        if 0 < vp < screen.lines:
            max_offset += screen.lines - vp
        new_offset = max(0, min(view.scroll_offset + lines, max_offset))
        if new_offset == view.scroll_offset:
            return
        view.scroll_offset = new_offset
        if new_offset == 0 and not self._view_needs_viewport(view, screen):
            from mutbot.ptyhost.ansi_render import render_full
            frame = render_full(screen)
        else:
            frame = self._render_viewport_frame(view, screen, new_offset)
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
        vp = view.viewport_rows
        if 0 < vp < screen.lines:
            max_offset += screen.lines - vp
        new_offset = max(0, min(offset, max_offset))
        if new_offset == view.scroll_offset:
            return
        view.scroll_offset = new_offset
        if new_offset == 0 and not self._view_needs_viewport(view, screen):
            from mutbot.ptyhost.ansi_render import render_full
            frame = render_full(screen)
        else:
            frame = self._render_viewport_frame(view, screen, new_offset)
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
        if not self._view_needs_viewport(view, term.screen):
            from mutbot.ptyhost.ansi_render import render_full
            frame = render_full(term.screen)
        else:
            frame = self._render_viewport_frame(view, term.screen, 0)
        if frame:
            self._on_frame(view.term_id, view.id, frame)

    def _view_needs_viewport(self, view: TermView, screen: _SafeHistoryScreen) -> bool:
        """判断 view 是否需要 viewport 模式（行或列小于终端尺寸）。"""
        vr = view.viewport_rows
        vc = view.viewport_cols
        return (0 < vr < screen.lines) or (0 < vc < screen.columns)

    def _render_viewport_frame(
        self, view: TermView, screen: _SafeHistoryScreen, offset: int,
    ) -> bytes:
        """渲染 viewport 帧：行裁剪 + 列裁剪。

        offset: scroll_offset（0 = live，>0 = 从底部往上行数）。
        """
        from mutbot.ptyhost.ansi_render import render_lines
        vr = view.viewport_rows
        visible = vr if 0 < vr < screen.lines else screen.lines
        vc = view.viewport_cols
        cols = vc if 0 < vc < screen.columns else screen.columns

        history_top = list(screen.history.top)
        buffer_lines = [screen.buffer[row] for row in range(screen.lines)]
        all_lines = history_top + buffer_lines
        total = len(all_lines)
        # offset 是从底部往上偏移
        end = total - offset
        start = max(0, end - visible)
        end = start + visible  # 确保刚好 visible 行
        lines_slice: list = all_lines[start:end]
        # 如果不够 visible 行，用空行补齐
        while len(lines_slice) < visible:
            lines_slice.append({})
        return render_lines(lines_slice, cols, screen.default_char)

    def clear_scrollback(self, term_id: str) -> None:
        """清除终端的 scrollback 历史缓冲，重置所有 view 的滚动偏移。"""
        term = self._terminals.get(term_id)
        if term is None or term.screen is None:
            return
        term.screen.history.top.clear()
        screen = term.screen
        for view in term.views.values():
            view.scroll_offset = 0
        # 全量渲染推送给所有 view
        from mutbot.ptyhost.ansi_render import render_full
        full_frame: bytes | None = None
        for view in term.views.values():
            if self._view_needs_viewport(view, screen):
                frame = self._render_viewport_frame(view, screen, 0)
            else:
                if full_frame is None:
                    full_frame = render_full(screen)
                frame = full_frame
            if frame:
                self._on_frame(term_id, view.id, frame)

    def get_scroll_state(self, view_id: str) -> dict[str, int] | None:
        """返回滚动状态用于 scrollbar。"""
        view = self._views.get(view_id)
        if view is None:
            return None
        term = self._terminals.get(view.term_id)
        if term is None or term.screen is None:
            return None
        screen = term.screen
        vp = view.viewport_rows
        visible = vp if 0 < vp < screen.lines else screen.lines
        # total = history + screen.lines（viewport 模式下 total 不变，
        # 但 visible 变小，scrollbar 比例自然正确）
        return {
            "offset": view.scroll_offset,
            "total": len(screen.history.top) + screen.lines,
            "visible": visible,
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
        max_handle = self._flush_max_handles.pop(term_id, None)
        if max_handle is not None:
            max_handle.cancel()
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
