"""Cross-platform PTY terminal manager."""

from __future__ import annotations

import logging
import os
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


@dataclass
class TerminalSession:
    id: str
    workspace_id: str
    rows: int
    cols: int
    process: Any = None
    reader_thread: threading.Thread | None = None
    alive: bool = True
    _fd: int | None = field(default=None, repr=False)


class TerminalManager:
    """Manage PTY terminal sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}

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

    def has(self, term_id: str) -> bool:
        return term_id in self._sessions

    def start_reader(self, term_id: str, loop: Any, on_output: Callable[[bytes], None]) -> None:
        session = self._sessions.get(term_id)
        if session is None or session.reader_thread is not None:
            return

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
                            loop.call_soon_threadsafe(on_output, data)
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
                            loop.call_soon_threadsafe(on_output, data)
                        except OSError:
                            break
            finally:
                session.alive = False
                logger.info("Terminal reader stopped: %s", term_id)

        t = threading.Thread(target=reader, daemon=True, name=f"term-reader-{term_id}")
        session.reader_thread = t
        t.start()

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

    def kill(self, term_id: str) -> None:
        session = self._sessions.pop(term_id, None)
        if session is None:
            return

        session.alive = False
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
