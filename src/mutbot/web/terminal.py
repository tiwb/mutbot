"""Cross-platform PTY terminal manager with lifecycle decoupling.

PTY processes survive WebSocket disconnects.  Clients attach/detach
as I/O channels; the PTY is only killed by an explicit ``kill()`` call
(DELETE API or server shutdown).
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import WebSocket

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# Scrollback buffer: keep the last 64 KB of PTY output for reconnection
SCROLLBACK_MAX = 64 * 1024


@dataclass
class TerminalSession:
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


class TerminalManager:
    """Manage PTY terminal sessions with multi-client support.

    PTY lifetime is decoupled from WebSocket connections:
    - ``create()`` spawns a PTY and starts the reader thread.
    - ``attach()`` registers a WebSocket as an I/O channel.
    - ``detach()`` removes a WebSocket without killing the PTY.
    - ``kill()`` explicitly destroys the PTY process.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        # Multi-client: term_id → set of (WebSocket, event_loop) pairs
        self._connections: dict[str, set[tuple[WebSocket, asyncio.AbstractEventLoop]]] = {}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create(self, workspace_id: str, rows: int, cols: int, cwd: str | None = None) -> TerminalSession:
        term_id = uuid.uuid4().hex[:12]
        session = TerminalSession(
            id=term_id,
            workspace_id=workspace_id,
            rows=rows,
            cols=cols,
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

    def _spawn_windows(self, session: TerminalSession, cwd: str) -> None:
        from winpty import PtyProcess  # type: ignore[import-untyped]

        shell = os.environ.get("COMSPEC", "cmd.exe")
        proc = PtyProcess.spawn(
            shell,
            dimensions=(session.rows, session.cols),
            cwd=cwd,
        )
        session.process = proc

    def _spawn_unix(self, session: TerminalSession, cwd: str) -> None:
        import pty
        import subprocess

        shell = os.environ.get("SHELL", "/bin/bash")
        master_fd, slave_fd = pty.openpty()

        proc = subprocess.Popen(
            [shell],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave_fd)

        session.process = proc
        session._fd = master_fd

    # ------------------------------------------------------------------
    # Connection management (attach / detach)
    # ------------------------------------------------------------------

    def attach(self, term_id: str, ws: WebSocket, loop: asyncio.AbstractEventLoop) -> None:
        """Register a WebSocket as an I/O channel for this terminal."""
        self._connections.setdefault(term_id, set()).add((ws, loop))
        count = len(self._connections[term_id])
        logger.info("Terminal %s: attached client (total=%d)", term_id, count)

    def detach(self, term_id: str, ws: WebSocket) -> None:
        """Remove a WebSocket without killing the PTY."""
        conns = self._connections.get(term_id)
        if conns:
            conns.discard(next(((w, l) for w, l in conns if w is ws), (None, None)))  # type: ignore[arg-type]
            # Clean up properly
            to_remove = {pair for pair in conns if pair[0] is ws}
            conns -= to_remove
            if not conns:
                del self._connections[term_id]
        count = len(self._connections.get(term_id, set()))
        logger.info("Terminal %s: detached client (remaining=%d)", term_id, count)

    def get_scrollback(self, term_id: str) -> bytes:
        """Return the scrollback buffer contents for replay on reconnect."""
        session = self._sessions.get(term_id)
        if session is None:
            return b""
        with session._scrollback_lock:
            return bytes(session._scrollback)

    def connection_count(self, term_id: str) -> int:
        return len(self._connections.get(term_id, set()))

    # ------------------------------------------------------------------
    # Reader thread (PTY output → scrollback + broadcast)
    # ------------------------------------------------------------------

    def _start_reader(self, session: TerminalSession) -> None:
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
                logger.info("Terminal reader stopped: %s (exit_code=%s)", term_id, exit_code)
                self._notify_process_exit(term_id, exit_code)

        t = threading.Thread(target=reader, daemon=True, name=f"term-reader-{term_id}")
        session.reader_thread = t
        t.start()

    def _on_pty_output(self, session: TerminalSession, data: bytes) -> None:
        """Handle PTY output: append to scrollback and broadcast to all clients."""
        # Append to scrollback buffer
        with session._scrollback_lock:
            session._scrollback.extend(data)
            # Trim to max size
            overflow = len(session._scrollback) - SCROLLBACK_MAX
            if overflow > 0:
                del session._scrollback[:overflow]

        # Broadcast to all connected WebSocket clients
        conns = self._connections.get(session.id)
        if not conns:
            return
        payload = b"\x01" + data
        dead: list[tuple[WebSocket, asyncio.AbstractEventLoop]] = []
        for ws, loop in list(conns):
            try:
                asyncio.run_coroutine_threadsafe(ws.send_bytes(payload), loop)
            except Exception:
                dead.append((ws, loop))
        for pair in dead:
            conns.discard(pair)

    def _make_exit_payload(self, exit_code: int | None = None) -> bytes:
        """Build a 0x04 exit signal payload, optionally including exit code."""
        if exit_code is not None:
            return b"\x04" + struct.pack(">i", exit_code)
        return b"\x04"

    def _notify_process_exit(self, term_id: str, exit_code: int | None = None) -> None:
        """Send process exit signal (0x04) to all attached WS clients.

        Called from the reader thread — uses run_coroutine_threadsafe.
        """
        conns = self._connections.get(term_id)
        if not conns:
            return
        payload = self._make_exit_payload(exit_code)
        for ws, loop in list(conns):
            try:
                asyncio.run_coroutine_threadsafe(ws.send_bytes(payload), loop)
            except Exception:
                pass

    async def async_notify_exit(self, term_id: str) -> None:
        """Send process exit signal (0x04) to all attached WS clients (async).

        Must be called from the event loop thread, before kill().
        Uses await for reliable delivery.
        """
        session = self._sessions.get(term_id)
        exit_code = session.exit_code if session else None
        conns = self._connections.get(term_id)
        if not conns:
            return
        payload = self._make_exit_payload(exit_code)
        for ws, _loop in list(conns):
            try:
                await ws.send_bytes(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def has(self, term_id: str) -> bool:
        return term_id in self._sessions

    def get(self, term_id: str) -> TerminalSession | None:
        return self._sessions.get(term_id)

    def list_by_workspace(self, workspace_id: str) -> list[TerminalSession]:
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

    def resize(self, term_id: str, rows: int, cols: int) -> None:
        session = self._sessions.get(term_id)
        if session is None or not session.alive:
            return

        session.rows = rows
        session.cols = cols

        if IS_WINDOWS:
            try:
                session.process.setwinsize(rows, cols)
            except Exception:
                logger.debug("Terminal resize failed: %s", term_id)
        else:
            import fcntl
            import struct
            import termios
            fd = session._fd
            if fd is not None:
                try:
                    winsize = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                except Exception:
                    logger.debug("Terminal resize failed: %s", term_id)

    # ------------------------------------------------------------------
    # Destroy
    # ------------------------------------------------------------------

    def kill(self, term_id: str) -> None:
        session = self._sessions.pop(term_id, None)
        if session is None:
            return

        session.alive = False

        # Clear all attached WS connections
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
            try:
                if session._fd is not None:
                    os.close(session._fd)
                    session._fd = None
            except OSError:
                pass
            try:
                if session.process:
                    session.process.terminate()
                    session.process.wait(timeout=2)
            except Exception:
                try:
                    session.process.kill()
                except Exception:
                    pass

    def kill_all(self) -> None:
        for term_id in list(self._sessions):
            self.kill(term_id)
