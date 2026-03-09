"""Cross-platform PTY terminal manager with lifecycle decoupling.

PTY processes survive client disconnects.  Clients attach/detach
as I/O channels; the PTY is only killed by an explicit ``kill()`` call
(DELETE API or server shutdown).

包含 TerminalSession 的所有 @impl：生命周期（on_create / on_stop / on_restart_cleanup）
+ Channel 通信（on_connect / on_disconnect / on_message / on_data）。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import select as _select
import signal
import struct
import sys
import threading
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

import mutobj
from mutobj import impl

if TYPE_CHECKING:
    from mutbot.channel import Channel, ChannelContext
    from mutbot.runtime.session_manager import SessionManager

# 输出回调类型：接收原始 PTY 输出字节
# 支持异步回调（旧模式：run_coroutine_threadsafe）和同步回调（新模式：直接调用）
OutputCallback = Callable[[bytes], Awaitable[None] | None]

# 退出回调类型：接收 exit_code（可为 None）
ExitCallback = Callable[[int | None], None]

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# Scrollback buffer: keep the last 64 KB of PTY output for reconnection
SCROLLBACK_MAX = 64 * 1024

# Strip OSC 0/1/2 (window/icon title) sequences from PTY output to prevent
# host terminal title from being overwritten.
_OSC_TITLE_RE = re.compile(rb"\x1b\][012];[^\x07\x1b]*(?:\x07|\x1b\\)")


@dataclass
class TerminalProcess:
    id: str
    workspace_id: str
    rows: int
    cols: int
    process: Any = None
    reader_thread: threading.Thread | None = None
    alive: bool = True
    exit_code: int | None = field(default=None, repr=False)
    _fd: int | None = field(default=None, repr=False)
    # Scrollback buffer for reconnection replay
    _scrollback: bytearray = field(default_factory=bytearray, repr=False)
    _scrollback_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Per-client reported sizes for multi-client min-size tracking
    _client_sizes: dict[str, tuple[int, int]] = field(default_factory=dict, repr=False)
    # Callback invoked from reader thread when PTY process dies
    _on_dead: Callable[[str, bytes], None] | None = field(default=None, repr=False)


class TerminalManager:
    """Manage PTY terminal sessions with multi-client support.

    PTY lifetime is decoupled from client connections:
    - ``create()`` spawns a PTY and starts the reader thread.
    - ``attach()`` registers an output callback as an I/O channel.
    - ``detach()`` removes a callback without killing the PTY.
    - ``kill()`` explicitly destroys the PTY process.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalProcess] = {}
        # Multi-client: term_id → {client_id: (on_output, on_exit, event_loop)}
        self._connections: dict[str, dict[str, tuple[OutputCallback, ExitCallback, asyncio.AbstractEventLoop]]] = {}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create(self, workspace_id: str, rows: int, cols: int, cwd: str | None = None,
               on_dead: Callable[[str, bytes], None] | None = None) -> TerminalProcess:
        term_id = uuid.uuid4().hex[:12]
        session = TerminalProcess(
            id=term_id,
            workspace_id=workspace_id,
            rows=rows,
            cols=cols,
            _on_dead=on_dead,
        )

        work_dir = cwd or os.getcwd()

        if IS_WINDOWS:
            self._spawn_windows(session, work_dir)
        else:
            self._spawn_unix(session, work_dir)

        self._sessions[term_id] = session
        logger.info("Terminal created: id=%s, workspace=%s, cwd=%s", term_id, workspace_id, work_dir)

        # Start reader immediately (writes to scrollback + broadcasts)
        self._start_reader(session)
        return session

    def _spawn_windows(self, session: TerminalProcess, cwd: str) -> None:
        from winpty import PtyProcess  # type: ignore[import-untyped]

        shell = os.environ.get("COMSPEC", "cmd.exe")
        proc = PtyProcess.spawn(
            shell,
            dimensions=(session.rows, session.cols),
            cwd=cwd,
        )
        session.process = proc

    def _spawn_unix(self, session: TerminalProcess, cwd: str) -> None:
        import fcntl
        import pty
        import subprocess
        import termios

        shell = os.environ.get("SHELL", "/bin/bash")
        master_fd, slave_fd = pty.openpty()

        # Set initial PTY window size before spawning so the shell (and vim)
        # start up knowing the correct dimensions.
        winsize = struct.pack("HHHH", session.rows, session.cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}

        def _preexec() -> None:
            os.setsid()
            # Make the slave PTY the controlling terminal of the new session.
            # Without this, programs like vim that rely on tcsetattr / tcgetpgrp
            # do not work correctly (they see no controlling terminal).
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

        session.process = proc
        session._fd = master_fd

    # ------------------------------------------------------------------
    # Connection management (attach / detach)
    # ------------------------------------------------------------------

    def attach(
        self,
        term_id: str,
        client_id: str,
        on_output: OutputCallback,
        on_exit: ExitCallback,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Register output/exit callbacks as an I/O channel for this terminal."""
        conns = self._connections.setdefault(term_id, {})
        conns[client_id] = (on_output, on_exit, loop)
        logger.info("Terminal %s: attached client %s (total=%d)", term_id, client_id, len(conns))

    def detach(self, term_id: str, client_id: str) -> tuple[int, int] | None:
        """Remove an output callback without killing the PTY.

        Returns the updated (rows, cols) after recalculating min size,
        or None if no remaining clients or session is dead.
        """
        result: tuple[int, int] | None = None
        session = self._sessions.get(term_id)
        if session:
            session._client_sizes.pop(client_id, None)
            # If other clients remain, reapply the new minimum size
            if session._client_sizes:
                result = self._apply_min_size(session)
        conns = self._connections.get(term_id)
        if conns:
            conns.pop(client_id, None)
            if not conns:
                del self._connections[term_id]
        remaining = len(self._connections.get(term_id, {}))
        logger.info("Terminal %s: detached client %s (remaining=%d)", term_id, client_id, remaining)
        return result

    def get_scrollback(self, term_id: str) -> bytes:
        """Return the scrollback buffer contents for replay on reconnect."""
        session = self._sessions.get(term_id)
        if session is None:
            return b""
        with session._scrollback_lock:
            return bytes(session._scrollback)

    def connection_count(self, term_id: str) -> int:
        return len(self._connections.get(term_id, {}))

    # ------------------------------------------------------------------
    # Reader thread (PTY output → scrollback + broadcast)
    # ------------------------------------------------------------------

    def _start_reader(self, session: TerminalProcess) -> None:
        """Start the background reader thread for a terminal session."""
        if session.reader_thread is not None:
            return

        term_id = session.id

        def reader():
            try:
                if IS_WINDOWS:
                    proc = session.process
                    while session.alive:
                        try:
                            data = proc.read(4096)
                            if not data:
                                break
                            if isinstance(data, str):
                                data = data.encode("utf-8", errors="replace")
                            self._on_pty_output(session, data)
                        except EOFError:
                            break
                        except Exception:
                            if not session.alive:
                                break
                            logger.exception("Terminal reader error: %s", term_id)
                            break
                else:
                    fd = session._fd
                    while session.alive and fd is not None:
                        try:
                            rlist, _, _ = _select.select([fd], [], [], 1.0)
                            if not rlist:
                                # Timeout — re-check alive flag and refresh fd
                                fd = session._fd
                                continue
                            data = os.read(fd, 4096)
                            if not data:
                                break
                            self._on_pty_output(session, data)
                        except OSError:
                            break
            finally:
                session.alive = False
                # Capture exit code
                exit_code = None
                try:
                    if IS_WINDOWS:
                        proc = session.process
                        if proc:
                            exit_code = getattr(proc, "exitstatus", None)
                    else:
                        proc = session.process
                        if proc:
                            proc.wait(timeout=1)
                            exit_code = proc.returncode
                except Exception:
                    pass
                session.exit_code = exit_code
                # Copy scrollback to session owner before notifying exit
                if session._on_dead:
                    with session._scrollback_lock:
                        scrollback_copy = bytes(session._scrollback)
                    session._on_dead(session.id, scrollback_copy)
                logger.info("Terminal reader stopped: %s (exit_code=%s)", term_id, exit_code)
                self._notify_process_exit(term_id, exit_code)

        t = threading.Thread(target=reader, daemon=True, name=f"term-reader-{term_id}")
        session.reader_thread = t
        t.start()

    def _on_pty_output(self, session: TerminalProcess, data: bytes) -> None:
        """Handle PTY output: strip title sequences, append to scrollback, broadcast."""
        # Strip OSC 0/1/2 title sequences before storing or forwarding
        data = _OSC_TITLE_RE.sub(b"", data)
        if not data:
            return

        # Append to scrollback buffer
        with session._scrollback_lock:
            session._scrollback.extend(data)
            # Trim to max size
            overflow = len(session._scrollback) - SCROLLBACK_MAX
            if overflow > 0:
                del session._scrollback[:overflow]

        # Broadcast to all connected clients via callbacks
        conns = self._connections.get(session.id)
        if not conns:
            return
        payload = data
        dead: list[str] = []
        for client_id, (on_output, _on_exit, loop) in list(conns.items()):
            try:
                result = on_output(payload)
                if result is not None:
                    # 异步回调 → run_coroutine_threadsafe
                    asyncio.run_coroutine_threadsafe(result, loop)
            except Exception:
                dead.append(client_id)
        for client_id in dead:
            conns.pop(client_id, None)

    def _notify_process_exit(self, term_id: str, exit_code: int | None = None) -> None:
        """Send process exit signal to all attached clients via on_exit callback.

        Called from the reader thread — on_exit is synchronous (put_nowait).
        """
        conns = self._connections.get(term_id)
        if not conns:
            return
        for _client_id, (_on_output, on_exit, _loop) in list(conns.items()):
            try:
                on_exit(exit_code)
            except Exception:
                pass

    async def async_notify_exit(self, term_id: str) -> None:
        """Send process exit signal to all attached clients via on_exit callback (async).

        Must be called from the event loop thread, before kill().
        """
        session = self._sessions.get(term_id)
        exit_code = session.exit_code if session else None
        conns = self._connections.get(term_id)
        if not conns:
            return
        for _client_id, (_on_output, on_exit, _loop) in list(conns.items()):
            try:
                on_exit(exit_code)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def has(self, term_id: str) -> bool:
        return term_id in self._sessions

    def get(self, term_id: str) -> TerminalProcess | None:
        return self._sessions.get(term_id)

    def list_by_workspace(self, workspace_id: str) -> list[TerminalProcess]:
        return [s for s in self._sessions.values() if s.workspace_id == workspace_id]

    # ------------------------------------------------------------------
    # I/O: write + resize (unchanged API)
    # ------------------------------------------------------------------

    def write(self, term_id: str, data: bytes) -> None:
        session = self._sessions.get(term_id)
        if session is None or not session.alive:
            return

        if IS_WINDOWS:
            proc = session.process
            try:
                if isinstance(data, bytes):
                    data_str = data.decode("utf-8", errors="replace")
                else:
                    data_str = data
                proc.write(data_str)
            except Exception:
                logger.exception("Terminal write error: %s", term_id)
        else:
            fd = session._fd
            if fd is not None:
                try:
                    os.write(fd, data)
                except OSError:
                    logger.exception("Terminal write error: %s", term_id)

    def resize(self, term_id: str, rows: int, cols: int, client_id: str | None = None) -> tuple[int, int] | None:
        """Resize the PTY, returning the actual (rows, cols) applied, or None."""
        session = self._sessions.get(term_id)
        if session is None or not session.alive:
            return None

        if client_id is not None:
            session._client_sizes[client_id] = (rows, cols)

        # Use the minimum size across all connected clients (tmux behaviour)
        if session._client_sizes:
            return self._apply_min_size(session)
        else:
            session.rows = rows
            session.cols = cols
            self._set_pty_size(session, rows, cols)
            return (rows, cols)

    def _apply_min_size(self, session: TerminalProcess) -> tuple[int, int] | None:
        """Recompute effective size as the minimum across all clients and apply."""
        if not session._client_sizes or not session.alive:
            return None
        eff_rows = min(r for r, _ in session._client_sizes.values())
        eff_cols = min(c for _, c in session._client_sizes.values())
        session.rows = eff_rows
        session.cols = eff_cols
        self._set_pty_size(session, eff_rows, eff_cols)
        return (eff_rows, eff_cols)

    def _set_pty_size(self, session: TerminalProcess, rows: int, cols: int) -> None:
        """Apply rows/cols to the underlying PTY device."""
        if IS_WINDOWS:
            try:
                session.process.setwinsize(rows, cols)
            except Exception:
                logger.debug("Terminal resize failed: %s", session.id)
        else:
            import fcntl
            import termios
            fd = session._fd
            if fd is not None:
                try:
                    winsize = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                except Exception:
                    logger.debug("Terminal resize failed: %s", session.id)

    # ------------------------------------------------------------------
    # Destroy
    # ------------------------------------------------------------------

    def kill(self, term_id: str) -> None:
        session = self._sessions.pop(term_id, None)
        if session is None:
            return

        session.alive = False

        # Clear all attached client connections
        # (callers should use async_notify_exit() before kill() to send 0x04)
        self._connections.pop(term_id, None)

        logger.info("Killing terminal: %s", term_id)

        if IS_WINDOWS:
            try:
                if session.process and session.process.isalive():
                    session.process.terminate()
            except Exception:
                pass
        else:
            # Kill the entire process group (shell + vim + all children).
            # On macOS, os.read(master_fd) does NOT unblock when the fd is
            # closed from another thread — we must kill the child processes
            # first so the slave PTY is closed, which causes EIO on the master.
            try:
                if session.process:
                    pgid = os.getpgid(session.process.pid)
                    os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    if session.process:
                        session.process.kill()
                except Exception:
                    pass
            # Close master fd after sending kill signal
            try:
                if session._fd is not None:
                    os.close(session._fd)
                    session._fd = None
            except OSError:
                pass

    def kill_all(self) -> None:
        for term_id in list(self._sessions):
            self.kill(term_id)

    def inject_scrollback(self, term_id: str, data: bytes) -> None:
        """Prepend historical scrollback data into a newly-created terminal's buffer.

        Used to restore persisted scrollback after a server restart.
        """
        session = self._sessions.get(term_id)
        if session is None or not data:
            return
        with session._scrollback_lock:
            combined = bytearray(data) + session._scrollback
            if len(combined) > SCROLLBACK_MAX:
                combined = combined[-SCROLLBACK_MAX:]
            session._scrollback = combined


# ---------------------------------------------------------------------------
# TerminalSession @impl — 生命周期
# ---------------------------------------------------------------------------

from mutbot.session import TerminalSession

_CSI_QUERY_RE = re.compile(rb"\x1b\[(?:[>=]?0?c|[56]n)")
_CLEAR_SCREEN = b"\x1b[0m\x1b[2J\x1b[H"


def _strip_replay_queries(data: bytes) -> bytes:
    """Strip CSI query sequences from scrollback data before replay."""
    return _CSI_QUERY_RE.sub(b"", data)


@impl(TerminalSession.on_create)
def _terminal_on_create(self: TerminalSession, sm: SessionManager) -> None:
    """TerminalSession：创建 PTY，恢复历史 scrollback，设 running。"""
    tm = sm.terminal_manager
    if tm is None:
        return
    rows = self.config.get("rows", 24)
    cols = self.config.get("cols", 80)
    cwd = self.config.get("cwd", ".")

    # on_dead: called from reader thread when PTY dies — copy scrollback + mark dirty
    session_id = self.id
    def on_dead(term_id: str, scrollback: bytes) -> None:
        self.scrollback_b64 = base64.b64encode(scrollback).decode()
        sm.mark_dirty(session_id)

    term = tm.create(self.workspace_id, rows, cols, cwd=cwd, on_dead=on_dead)
    self.config["terminal_id"] = term.id

    # Restore persisted scrollback from previous session
    if self.scrollback_b64:
        try:
            data = base64.b64decode(self.scrollback_b64)
            tm.inject_scrollback(term.id, data)
        except Exception:
            pass
        self.scrollback_b64 = ""

    self.status = "running"


@impl(TerminalSession.on_stop)
def _terminal_on_stop(self: TerminalSession, sm: SessionManager) -> None:
    """TerminalSession：persist scrollback, kill PTY, set stopped."""
    tm = sm.terminal_manager
    if tm is not None and self.config:
        terminal_id = self.config.get("terminal_id")
        if terminal_id and tm.has(terminal_id):
            scrollback = tm.get_scrollback(terminal_id)
            if scrollback:
                self.scrollback_b64 = base64.b64encode(scrollback).decode()
            tm.kill(terminal_id)
    self.status = "stopped"


@impl(TerminalSession.on_restart_cleanup)
def _terminal_on_restart_cleanup(self: TerminalSession) -> None:
    """TerminalSession：running → stopped。"""
    if self.status == "running":
        self.status = "stopped"


# ---------------------------------------------------------------------------
# TerminalSession @impl — Channel 通信
# ---------------------------------------------------------------------------


@impl(TerminalSession.on_connect)
def _terminal_on_connect(self: TerminalSession, channel: Channel, ctx: ChannelContext) -> None:
    """attach PTY + scrollback replay + 发送 ready。"""
    term_id = self.config.get("terminal_id", "")
    tm = ctx.terminal_manager
    if not tm or not term_id:
        return

    # ---- 发送 scrollback（始终先清屏） ----
    ts = tm.get(term_id)
    if ts:
        scrollback = tm.get_scrollback(term_id)
        scrollback = _strip_replay_queries(scrollback) if scrollback else b""
        channel.send_binary(_CLEAR_SCREEN + scrollback)
    elif self.scrollback_b64:
        try:
            scrollback_data = base64.b64decode(self.scrollback_b64)
            scrollback_data = _strip_replay_queries(scrollback_data) if scrollback_data else b""
            channel.send_binary(_CLEAR_SCREEN + scrollback_data)
        except Exception:
            logger.warning("scrollback decode failed", exc_info=True)
            channel.send_binary(_CLEAR_SCREEN)
    else:
        channel.send_binary(_CLEAR_SCREEN)

    # ---- 判断终端状态，发送 ready ----
    alive = ts is not None and ts.alive
    if alive:
        channel.send_json({"type": "ready", "alive": True})
        # 获取 client_id 用于 attach（通过 ChannelTransport）
        from mutbot.web.transport import ChannelTransport
        ext = ChannelTransport.get(channel)
        client_id = ext._client.client_id if ext and ext._client else ""

        def on_output(data: bytes) -> None:
            channel.send_binary(data)

        def on_exit(exit_code: int | None) -> None:
            event: dict = {"type": "process_exit"}
            if exit_code is not None:
                event["exit_code"] = exit_code
            channel.send_json(event)

        tm.attach(term_id, client_id, on_output, on_exit, ctx.event_loop)
    else:
        event: dict = {"type": "ready", "alive": False}
        if ts and ts.exit_code is not None:
            event["exit_code"] = ts.exit_code
        channel.send_json(event)


@impl(TerminalSession.on_disconnect)
def _terminal_on_disconnect(self: TerminalSession, channel: Channel, ctx: ChannelContext) -> None:
    """detach PTY。"""
    term_id = self.config.get("terminal_id", "")
    tm = ctx.terminal_manager
    if not tm or not term_id:
        return
    from mutbot.web.transport import ChannelTransport
    ext = ChannelTransport.get(channel)
    client_id = ext._client.client_id if ext and ext._client else ""
    if client_id:
        new_size = tm.detach(term_id, client_id)
        if new_size is not None:
            self.broadcast_json({"type": "pty_resize", "rows": new_size[0], "cols": new_size[1]})


@impl(TerminalSession.on_message)
async def _terminal_on_message(self: TerminalSession, channel: Channel, raw: dict, ctx: ChannelContext) -> None:
    """处理 resize。"""
    if raw.get("type") == "resize":
        tm = ctx.terminal_manager
        term_id = self.config.get("terminal_id", "")
        if tm and term_id and tm.has(term_id):
            from mutbot.web.transport import ChannelTransport
            ext = ChannelTransport.get(channel)
            client_id = ext._client.client_id if ext and ext._client else ""
            actual = tm.resize(term_id, raw.get("rows", 24), raw.get("cols", 80), client_id=client_id)
            if actual is not None:
                self.broadcast_json({"type": "pty_resize", "rows": actual[0], "cols": actual[1]})


@impl(TerminalSession.on_data)
async def _terminal_on_data(self: TerminalSession, channel: Channel, payload: bytes, ctx: ChannelContext) -> None:
    """键盘输入转发到 PTY。"""
    term_id = self.config.get("terminal_id", "")
    tm = ctx.terminal_manager
    if tm and term_id and tm.has(term_id) and len(payload) > 0:
        tm.write(term_id, payload)
