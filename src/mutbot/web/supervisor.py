"""Supervisor 进程管理 — TCP 代理 + Worker 生命周期管理 + 管理 API。

Supervisor 是主进程，绑定公网端口，将流量 TCP 透传给 Worker 子进程。
支持热重启：drain 旧 Worker → spawn 新 Worker → 路由切换。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import signal
import socket as _socket
import subprocess
import sys
import time
import traceback
from typing import Any

logger = logging.getLogger("mutbot.supervisor")

# 管理路径前缀
_MANAGEMENT_PATHS = (b"/api/restart", b"/api/eval", b"/health")
_INTERNAL_PREFIX = b"/internal/"

# Worker 健康检查
_HEALTH_CHECK_INTERVAL = 0.3  # 秒
_HEALTH_CHECK_TIMEOUT = 30.0  # 秒

# Drain 超时
_DRAIN_TIMEOUT = 300.0  # 5 分钟

# Worker 崩溃后重启延迟
_CRASH_RESTART_DELAY = 1.0  # 秒


def _find_free_port() -> int:
    """找一个可用的 localhost 端口。"""
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WorkerProcess:
    """管理一个 Worker 子进程。"""

    def __init__(self, port: int, proc: subprocess.Popen[Any], generation: int):
        self.port = port
        self.proc = proc
        self.generation = generation
        self.ready = False
        self.draining = False
        # Supervisor 跟踪的代理连接数
        self.active_connections = 0

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def terminate(self) -> None:
        if self.alive:
            self.proc.terminate()

    def kill(self) -> None:
        if self.alive:
            self.proc.kill()


class Supervisor:
    """Supervisor 主逻辑：TCP 代理 + Worker 管理 + 管理 API。"""

    def __init__(
        self,
        *,
        listen_addresses: list[tuple[str, int]],
        worker_args: list[str],
        debug: bool = False,
    ):
        self.listen_addresses = listen_addresses
        self.worker_args = worker_args
        self.debug = debug

        self._servers: list[asyncio.AbstractServer] = []
        self._active_worker: WorkerProcess | None = None
        self._old_workers: list[WorkerProcess] = []
        self._generation = 0
        self._restarting = False
        self._should_exit = False
        self._force_exit = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._restart_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(self) -> None:
        """阻塞运行 Supervisor。"""
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._serve())
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()

        # 安装信号处理
        self._install_signal_handlers()

        # 启动 TCP 服务器
        for host, port in self.listen_addresses:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError as e:
                logger.error("Failed to bind %s:%d — %s", host, port, e)
                sock.close()
                raise
            server = await asyncio.start_server(
                self._handle_connection, sock=sock,
            )
            self._servers.append(server)
            logger.info("Supervisor listening on %s:%d", host, port)

        # Spawn 第一个 Worker
        await self._spawn_worker()

        # 启动 Worker 存活监控
        self._monitor_task = asyncio.create_task(self._monitor_workers())

        # Banner
        self._print_banner()

        # 主循环
        while not self._should_exit:
            await asyncio.sleep(0.2)

        # 优雅退出
        await self._shutdown()

    # ------------------------------------------------------------------
    # Banner
    # ------------------------------------------------------------------

    def _print_banner(self) -> None:
        import mutbot
        from mutbot.web.server import _build_banner_lines
        lines = _build_banner_lines(self.listen_addresses)
        print(f"\n  MutBot v{mutbot.__version__} (supervisor mode)\n")
        for line in lines:
            print(line)
        print()

    # ------------------------------------------------------------------
    # 信号处理
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        if sys.platform == "win32":
            signal.signal(signal.SIGINT, self._handle_signal_sync)
            signal.signal(signal.SIGBREAK, self._handle_signal_sync)  # type: ignore[attr-defined]
        else:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, self._handle_signal)
            loop.add_signal_handler(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self) -> None:
        if self._should_exit:
            self._force_exit = True
            logger.warning("Force shutdown requested")
            print("\nForce shutting down...", flush=True)
            # 强制 kill 所有 Worker
            for w in [self._active_worker] + self._old_workers:
                if w and w.alive:
                    w.kill()
            os._exit(0)
        self._should_exit = True
        logger.info("Graceful shutdown requested (Ctrl+C)")
        print("\nShutting down gracefully... Press Ctrl+C again to force exit", flush=True)

    def _handle_signal_sync(self, signum: int, frame: Any) -> None:
        self._handle_signal()

    # ------------------------------------------------------------------
    # Worker 管理
    # ------------------------------------------------------------------

    async def _spawn_worker(self) -> WorkerProcess:
        """Spawn 一个新的 Worker 子进程。"""
        self._generation += 1
        port = _find_free_port()

        cmd = [sys.executable, "-m", "mutbot", "--worker", "--port", str(port)]
        cmd.extend(self.worker_args)

        logger.info("Spawning Worker gen=%d on port %d: %s", self._generation, port, " ".join(cmd))

        # Windows 上需要 CREATE_NEW_PROCESS_GROUP 才能单独管理子进程
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(
            cmd,
            stdout=sys.stdout,
            stderr=sys.stderr,
            **kwargs,
        )

        worker = WorkerProcess(port, proc, self._generation)

        # 等待 Worker 就绪
        if await self._wait_worker_ready(worker):
            worker.ready = True
            self._active_worker = worker
            logger.info("Worker gen=%d ready on port %d (pid=%d)", worker.generation, port, proc.pid)
        else:
            logger.error("Worker gen=%d failed to become ready, killing", worker.generation)
            worker.kill()
            raise RuntimeError(f"Worker failed to start on port {port}")

        return worker

    async def _wait_worker_ready(self, worker: WorkerProcess, timeout: float = _HEALTH_CHECK_TIMEOUT) -> bool:
        """轮询 Worker 的 /health 端点直到返回 200。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not worker.alive:
                return False
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", worker.port)
                writer.write(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                await writer.drain()
                response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                if b"200" in response[:20]:
                    return True
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
        return False

    async def _monitor_workers(self) -> None:
        """监控 Worker 存活状态，崩溃时自动重启。"""
        while not self._should_exit:
            await asyncio.sleep(1.0)

            # 检查活跃 Worker
            worker = self._active_worker
            if worker and not worker.alive and not worker.draining and not self._restarting:
                rc = worker.proc.returncode
                logger.warning("Worker gen=%d (pid=%d) exited unexpectedly (rc=%s), restarting...",
                               worker.generation, worker.proc.pid, rc)
                self._active_worker = None
                await asyncio.sleep(_CRASH_RESTART_DELAY)
                if not self._should_exit:
                    try:
                        await self._spawn_worker()
                    except Exception:
                        logger.exception("Failed to restart Worker after crash")

            # 清理已退出的旧 Worker
            self._old_workers = [w for w in self._old_workers if w.alive]

    # ------------------------------------------------------------------
    # TCP 代理
    # ------------------------------------------------------------------

    async def _handle_connection(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """处理一个客户端 TCP 连接：peek 第一行，决定路由。"""
        try:
            # 读取第一行（HTTP 请求行）
            first_line = await asyncio.wait_for(client_reader.readline(), timeout=10.0)
            if not first_line:
                client_writer.close()
                return

            # 解析请求路径
            path = self._parse_request_path(first_line)

            # 管理路径 → Supervisor 自己处理
            if path and self._is_management_path(path):
                await self._handle_management(path, first_line, client_reader, client_writer)
                return

            # /internal/ 前缀 → 外部请求拒绝
            if path and path.startswith(_INTERNAL_PREFIX):
                await self._send_http_response(client_writer, 403, "Forbidden")
                return

            # TCP 透传给 Worker
            worker = self._active_worker
            if not worker or not worker.ready or not worker.alive:
                await self._send_http_response(client_writer, 503, "Service Restarting",
                                               body=b'{"status":"restarting","message":"Server is starting up, please retry shortly."}',
                                               content_type="application/json")
                return

            await self._proxy_to_worker(worker, first_line, client_reader, client_writer)

        except asyncio.TimeoutError:
            try:
                client_writer.close()
            except Exception:
                pass
        except Exception:
            logger.debug("Connection handler error", exc_info=True)
            try:
                client_writer.close()
            except Exception:
                pass

    def _parse_request_path(self, first_line: bytes) -> bytes | None:
        """从 HTTP 请求行提取路径。如 b'GET /health HTTP/1.1\\r\\n' → b'/health'"""
        parts = first_line.split(b" ", 2)
        if len(parts) >= 2:
            return parts[1]
        return None

    def _is_management_path(self, path: bytes) -> bool:
        """判断是否为管理路径。"""
        for prefix in _MANAGEMENT_PATHS:
            if path == prefix or path.startswith(prefix + b"?") or path.startswith(prefix + b"/"):
                return True
        return False

    async def _proxy_to_worker(
        self,
        worker: WorkerProcess,
        first_line: bytes,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """TCP 透传：客户端 ↔ Worker 双向 pipe。"""
        try:
            w_reader, w_writer = await asyncio.open_connection("127.0.0.1", worker.port)
        except (ConnectionRefusedError, OSError):
            await self._send_http_response(client_writer, 503, "Service Unavailable")
            return

        worker.active_connections += 1
        try:
            # 补发偷看的第一行
            w_writer.write(first_line)
            await w_writer.drain()

            # 双向 pipe — 任一方向结束即取消另一个
            t1 = asyncio.create_task(self._pipe(client_reader, w_writer))
            t2 = asyncio.create_task(self._pipe(w_reader, client_writer))
            done, pending = await asyncio.wait(
                {t1, t2}, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        finally:
            worker.active_connections -= 1
            for writer in (w_writer, client_writer):
                try:
                    if not writer.is_closing():
                        writer.close()
                except Exception:
                    pass

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """单向数据管道。"""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except asyncio.CancelledError:
            pass
        finally:
            try:
                if not writer.is_closing():
                    writer.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 管理 API
    # ------------------------------------------------------------------

    async def _handle_management(
        self,
        path: bytes,
        first_line: bytes,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """处理管理路径请求。"""
        # 读取 HTTP headers，提取 Content-Length
        content_length = 0
        while True:
            line = await asyncio.wait_for(client_reader.readline(), timeout=5.0)
            if line in (b"\r\n", b"\n", b""):
                break
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())

        method = first_line.split(b" ", 1)[0].upper()

        if path == b"/health" or path.startswith(b"/health?"):
            await self._handle_health(client_writer)
        elif path == b"/api/restart" or path.startswith(b"/api/restart?"):
            # 安全检查：仅允许 localhost 访问 restart
            peername = client_writer.get_extra_info("peername")
            remote_ip = peername[0] if peername else ""
            if remote_ip not in ("127.0.0.1", "::1"):
                await self._send_http_response(client_writer, 403, "Forbidden")
                return
            if method == b"POST":
                await self._handle_restart(client_writer)
            else:
                await self._send_http_response(client_writer, 405, "Method Not Allowed")
        elif path == b"/api/eval" or path.startswith(b"/api/eval?"):
            peername = client_writer.get_extra_info("peername")
            remote_ip = peername[0] if peername else ""
            if remote_ip not in ("127.0.0.1", "::1"):
                await self._send_http_response(client_writer, 403, "Forbidden")
                return
            if method == b"POST":
                body = b""
                if content_length > 0:
                    body = await asyncio.wait_for(client_reader.readexactly(content_length), timeout=5.0)
                await self._handle_eval(client_writer, body)
            else:
                await self._send_http_response(client_writer, 405, "Method Not Allowed")
        else:
            await self._send_http_response(client_writer, 404, "Not Found")

    async def _handle_health(self, writer: asyncio.StreamWriter) -> None:
        """GET /health — 返回 Supervisor 和 Worker 状态。"""
        import mutbot
        worker = self._active_worker
        worker_info: dict[str, Any] = {"status": "none"}
        if worker:
            worker_info = {
                "status": "ready" if worker.ready else "starting",
                "pid": worker.proc.pid if worker.alive else None,
                "port": worker.port,
                "generation": worker.generation,
                "active_connections": worker.active_connections,
                "draining": worker.draining,
            }
        data = {
            "status": "ok",
            "version": mutbot.__version__,
            "mode": "supervisor",
            "pid": os.getpid(),
            "worker": worker_info,
            "restarting": self._restarting,
        }
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        await self._send_http_response(writer, 200, "OK", body=body, content_type="application/json")

    async def _handle_restart(self, writer: asyncio.StreamWriter) -> None:
        """POST /api/restart — 触发 Worker 热重启。"""
        if self._restarting:
            body = json.dumps({"status": "already_restarting"}).encode("utf-8")
            await self._send_http_response(writer, 409, "Conflict", body=body, content_type="application/json")
            return

        # 先返回响应（不阻塞客户端）
        body = json.dumps({"status": "restarting"}).encode("utf-8")
        await self._send_http_response(writer, 200, "OK", body=body, content_type="application/json")

        # 后台执行重启
        self._restart_task = asyncio.create_task(self._do_restart())

    async def _handle_eval(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        """POST /api/eval — 在 Supervisor 进程中执行 Python 代码。"""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            await self._send_http_response(writer, 400, "Bad Request")
            return

        code = data.get("code", "")
        if not code:
            await self._send_http_response(writer, 400, "Bad Request",
                                           body=b'{"error":"missing code"}', content_type="application/json")
            return

        namespace: dict[str, Any] = {
            "__builtins__": __builtins__,
            "supervisor": self,
            "active_worker": self._active_worker,
            "old_workers": self._old_workers,
            "generation": self._generation,
            "restarting": self._restarting,
        }

        try:
            result = eval(code, namespace)
            result_str = repr(result)
        except SyntaxError:
            buf = io.StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = buf
                exec(code, namespace)
                result_str = buf.getvalue() or "(no output)"
            except Exception:
                result_str = buf.getvalue() + traceback.format_exc()
            finally:
                sys.stdout = old_stdout
        except Exception:
            result_str = traceback.format_exc()

        resp_body = result_str.encode("utf-8")
        await self._send_http_response(writer, 200, "OK", body=resp_body, content_type="text/plain; charset=utf-8")

    async def _do_restart(self) -> None:
        """执行完整的热重启流程。"""
        self._restarting = True
        old_worker = self._active_worker
        try:
            # 1. Drain 旧 Worker
            if old_worker and old_worker.alive:
                old_worker.draining = True
                logger.info("Draining Worker gen=%d (pid=%d)", old_worker.generation, old_worker.proc.pid)
                drained = await self._drain_worker(old_worker)
                if not drained:
                    logger.warning("Drain failed for Worker gen=%d, continuing with restart", old_worker.generation)

            # 2. Spawn 新 Worker
            logger.info("Spawning new Worker...")
            try:
                new_worker = await self._spawn_worker()
            except Exception:
                logger.exception("Failed to spawn new Worker during restart")
                # 回滚：如果旧 Worker 还在就恢复
                if old_worker and old_worker.alive:
                    old_worker.draining = False
                    self._active_worker = old_worker
                self._restarting = False
                return

            # 3. 路由已切换（_spawn_worker 设置了 _active_worker）
            logger.info("Route switched to Worker gen=%d", new_worker.generation)

            # 4. 等待旧 Worker 退出
            if old_worker and old_worker.alive:
                self._old_workers.append(old_worker)
                asyncio.create_task(self._wait_old_worker_exit(old_worker))

        except Exception:
            logger.exception("Restart failed")
        finally:
            self._restarting = False

    async def _drain_worker(self, worker: WorkerProcess) -> bool:
        """通知 Worker 进入 drain 模式（直接调 Worker 内部端口）。"""
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", worker.port)
            request = (
                b"POST /internal/drain HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=30.0)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return b"200" in response[:20]
        except Exception:
            logger.warning("Failed to drain Worker gen=%d", worker.generation, exc_info=True)
            return False

    async def _wait_old_worker_exit(self, worker: WorkerProcess) -> None:
        """等待旧 Worker 退出：连接归零或超时后强制关闭。"""
        deadline = time.monotonic() + _DRAIN_TIMEOUT
        while time.monotonic() < deadline:
            if not worker.alive:
                logger.info("Old Worker gen=%d exited normally", worker.generation)
                return
            if worker.active_connections <= 0:
                logger.info("Old Worker gen=%d has no active connections, terminating", worker.generation)
                worker.terminate()
                # 等待进程退出
                for _ in range(50):  # 5 秒
                    if not worker.alive:
                        break
                    await asyncio.sleep(0.1)
                if worker.alive:
                    worker.kill()
                return
            await asyncio.sleep(1.0)

        # 超时强制关闭
        logger.warning("Old Worker gen=%d drain timeout (%.0fs), forcing kill", worker.generation, _DRAIN_TIMEOUT)
        worker.terminate()
        await asyncio.sleep(2.0)
        if worker.alive:
            worker.kill()

    # ------------------------------------------------------------------
    # HTTP 响应辅助
    # ------------------------------------------------------------------

    async def _send_http_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        *,
        body: bytes = b"",
        content_type: str = "text/plain",
    ) -> None:
        """发送极简 HTTP 响应。"""
        if not body and status != 200:
            body = f'{{"error":"{reason}"}}'.encode("utf-8")
            content_type = "application/json"
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8") + body
        try:
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 优雅退出
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Supervisor 优雅退出。"""
        logger.info("Supervisor shutting down...")

        # 取消监控任务
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # 关闭 TCP 服务器（停止 accept 新连接）
        for server in self._servers:
            server.close()
        logger.info("TCP servers closed (no new connections)")

        # 先 drain 和 terminate Worker（必须在 wait_closed 之前，
        # 否则 wait_closed 等待代理连接关闭 → 而连接依赖 Worker → 死锁）
        worker = self._active_worker
        if worker and worker.alive:
            logger.info("Draining active Worker gen=%d (pid=%d) before exit",
                        worker.generation, worker.proc.pid)
            await self._drain_worker(worker)
            logger.info("Terminating Worker gen=%d", worker.generation)
            worker.terminate()
            # 等待进程退出
            for _ in range(100):  # 10 秒
                if not worker.alive:
                    break
                await asyncio.sleep(0.1)
            if worker.alive:
                logger.warning("Worker gen=%d did not exit in time, killing", worker.generation)
                worker.kill()
            else:
                logger.info("Worker gen=%d exited normally", worker.generation)

        # 强制 kill 旧 Worker
        for w in self._old_workers:
            if w.alive:
                logger.info("Killing old Worker gen=%d", w.generation)
                w.kill()

        # Worker 已停止，代理连接会自然断开，等一小会儿让 TCP server 清理
        for server in self._servers:
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.debug("TCP server wait_closed timed out, continuing")

        logger.info("Supervisor stopped")
