"""PTY 进程池 — ptyhost 守护进程的核心。

从 mutbot.runtime.terminal 提取并简化：
- 去掉 workspace_id、per-client attach/detach（那是 mutbot 侧的事）
- term_id 使用 UUID hex (32 chars)
- 输出/退出通过 manager 级别回调通知上层
"""

from __future__ import annotations

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

logger = logging.getLogger("mutbot.ptyhost")

IS_WINDOWS = sys.platform == "win32"

SCROLLBACK_MAX = 64 * 1024

# Strip OSC 0/1/2 (window/icon title) sequences
_OSC_TITLE_RE = re.compile(rb"\x1b\][012];[^\x07\x1b]*(?:\x07|\x1b\\)")

# 回调类型
OutputCallback = Callable[[str, bytes], None]   # (term_id, data)
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
    _scrollback: bytearray = field(default_factory=bytearray, repr=False)
    _scrollback_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class TerminalManager:
    """PTY 进程池。

    纯粹管理 PTY 进程的创建、I/O、销毁，不管任何业务逻辑。
    输出和退出事件通过回调通知上层（PtyHostApp）。
    """

    def __init__(
        self,
        on_output: OutputCallback,
        on_exit: ExitCallback,
    ) -> None:
        self._terminals: dict[str, TerminalProcess] = {}
        self._on_output = on_output
        self._on_exit = on_exit

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(self, rows: int, cols: int, cwd: str | None = None) -> str:
        """创建 PTY 进程，返回 term_id (UUID hex)。"""
        term_id = uuid.uuid4().hex
        term = TerminalProcess(id=term_id, rows=rows, cols=cols)
        work_dir = cwd or os.getcwd()

        if IS_WINDOWS:
            self._spawn_windows(term, work_dir)
        else:
            self._spawn_unix(term, work_dir)

        self._terminals[term_id] = term
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
                            data = proc.read(4096)
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
                            data = os.read(fd, 4096)
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

        with term._scrollback_lock:
            term._scrollback.extend(data)
            overflow = len(term._scrollback) - SCROLLBACK_MAX
            if overflow > 0:
                del term._scrollback[:overflow]

        self._on_output(term.id, data)

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
    # Query
    # ------------------------------------------------------------------

    def get_scrollback(self, term_id: str) -> bytes:
        term = self._terminals.get(term_id)
        if term is None:
            return b""
        with term._scrollback_lock:
            return bytes(term._scrollback)

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
