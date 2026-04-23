"""Supervisor 进程管理测试

涵盖：
- TCP 代理透传（HTTP + WebSocket）
- 管理路径识别和拦截
- /internal/ 前缀外部请求 403
- 无 Worker 时返回 503
- Worker spawn + 健康检查
- /health 端点
- /api/restart 端点
- 重启流程
"""

from __future__ import annotations

import asyncio
import json
import socket
from unittest.mock import MagicMock

import pytest

from mutbot.web.supervisor import Supervisor, WorkerProcess, _find_free_port


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_PORT = 19500


async def _start_echo_server(port: int) -> asyncio.AbstractServer:
    """启动一个简单的 echo HTTP server，模拟 Worker。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            first_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            # supervisor 发送 PROXY protocol v1 header（"PROXY TCP4 ..."），
            # echo server 不处理 PROXY，跳过后读取真正的 HTTP request line
            if first_line.startswith(b"PROXY "):
                first_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            # 读取剩余 headers
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            path = first_line.split(b" ", 2)[1] if b" " in first_line else b"/"

            if path == b"/api/health":
                body = b'{"status":"ok"}'
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n"
                    b"\r\n" + body
                )
            elif path == b"/internal/drain":
                body = b'{"status":"drained"}'
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n"
                    b"\r\n" + body
                )
            else:
                body = b"echo:" + path
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n"
                    b"\r\n" + body
                )
            writer.write(resp)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    return server


async def _http_get(port: int, path: str = "/") -> tuple[int, bytes]:
    """发送简单 HTTP GET，返回 (status, body)。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        request = f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.read(65536), timeout=5.0)
        # 解析 status
        first_line = response.split(b"\r\n", 1)[0]
        status = int(first_line.split(b" ", 2)[1])
        # 解析 body
        body_start = response.find(b"\r\n\r\n")
        body = response[body_start + 4:] if body_start >= 0 else b""
        return status, body
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _http_post(port: int, path: str = "/") -> tuple[int, bytes]:
    """发送简单 HTTP POST，返回 (status, body)。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        request = f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.read(65536), timeout=5.0)
        first_line = response.split(b"\r\n", 1)[0]
        status = int(first_line.split(b" ", 2)[1])
        body_start = response.find(b"\r\n\r\n")
        body = response[body_start + 4:] if body_start >= 0 else b""
        return status, body
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Supervisor 单元测试（不 spawn 真实 Worker）
# ---------------------------------------------------------------------------

class TestSupervisorUnit:
    """Supervisor 核心逻辑单元测试（mock Worker 进程）。"""

    def test_find_free_port(self):
        """_find_free_port 返回可用端口。"""
        port = _find_free_port()
        assert 1024 < port < 65536
        # 端口应该可以绑定
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))

    def test_worker_process_state(self):
        """WorkerProcess 状态管理。"""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # 进程运行中
        worker = WorkerProcess(port=9999, proc=mock_proc, generation=1)
        assert worker.alive
        assert worker.port == 9999
        assert worker.generation == 1
        assert not worker.ready
        assert not worker.draining
        assert worker.active_connections == 0

    def test_worker_process_dead(self):
        """WorkerProcess 进程已退出。"""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # 进程已退出
        worker = WorkerProcess(port=9999, proc=mock_proc, generation=1)
        assert not worker.alive

    def test_parse_request_path(self):
        """解析 HTTP 请求行中的路径。"""
        sup = Supervisor(listen_addresses=[], worker_args=[])
        assert sup._parse_request_path(b"GET /health HTTP/1.1\r\n") == b"/health"
        assert sup._parse_request_path(b"POST /api/restart HTTP/1.1\r\n") == b"/api/restart"
        assert sup._parse_request_path(b"GET / HTTP/1.1\r\n") == b"/"
        assert sup._parse_request_path(b"INVALID") is None

    def test_is_management_path(self):
        """管理路径识别。"""
        sup = Supervisor(listen_addresses=[], worker_args=[])
        assert sup._is_management_path(b"/health")
        assert sup._is_management_path(b"/health?check=1")
        assert sup._is_management_path(b"/api/restart")
        assert sup._is_management_path(b"/api/restart?force=1")
        assert not sup._is_management_path(b"/")
        assert not sup._is_management_path(b"/ws/app")
        assert not sup._is_management_path(b"/api/other")
        assert not sup._is_management_path(b"/internal/drain")


# ---------------------------------------------------------------------------
# Supervisor TCP 代理测试（用 mock echo server 模拟 Worker）
# ---------------------------------------------------------------------------

class TestSupervisorProxy:
    """Supervisor TCP 代理功能测试。"""

    @pytest.mark.asyncio
    async def test_proxy_to_worker(self):
        """TCP 代理透传请求到 Worker。"""
        worker_port = _find_free_port()
        sup_port = _find_free_port()

        # 启动 mock Worker
        echo_server = await _start_echo_server(worker_port)

        # 创建 Supervisor 并手动设置 Worker
        sup = Supervisor(listen_addresses=[(("127.0.0.1"), sup_port)], worker_args=[])

        try:
            # 手动启动 Supervisor TCP 服务器
            server = await asyncio.start_server(
                sup._handle_connection, host="127.0.0.1", port=sup_port,
            )
            sup._servers.append(server)

            # 设置 mock Worker
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.pid = 12345
            worker = WorkerProcess(port=worker_port, proc=mock_proc, generation=1)
            worker.ready = True
            sup._active_worker = worker

            # 发送请求
            status, body = await _http_get(sup_port, "/test/path")
            assert status == 200
            assert body == b"echo:/test/path"

        finally:
            for s in sup._servers:
                s.close()
                await s.wait_closed()
            echo_server.close()
            await echo_server.wait_closed()

    @pytest.mark.asyncio
    async def test_no_worker_returns_503(self):
        """无 Worker 时返回 503。"""
        sup_port = _find_free_port()
        sup = Supervisor(listen_addresses=[("127.0.0.1", sup_port)], worker_args=[])

        try:
            server = await asyncio.start_server(
                sup._handle_connection, host="127.0.0.1", port=sup_port,
            )
            sup._servers.append(server)
            # 不设置 Worker
            sup._active_worker = None

            status, body = await _http_get(sup_port, "/anything")
            assert status == 503
            data = json.loads(body)
            assert data["status"] == "restarting"

        finally:
            for s in sup._servers:
                s.close()
                await s.wait_closed()

    @pytest.mark.asyncio
    async def test_internal_prefix_blocked(self):
        """/internal/ 前缀外部请求返回 403。"""
        sup_port = _find_free_port()
        sup = Supervisor(listen_addresses=[("127.0.0.1", sup_port)], worker_args=[])

        try:
            server = await asyncio.start_server(
                sup._handle_connection, host="127.0.0.1", port=sup_port,
            )
            sup._servers.append(server)

            # 设置 mock Worker
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            worker = WorkerProcess(port=9999, proc=mock_proc, generation=1)
            worker.ready = True
            sup._active_worker = worker

            status, body = await _http_get(sup_port, "/internal/drain")
            assert status == 403

        finally:
            for s in sup._servers:
                s.close()
                await s.wait_closed()

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        """/health 返回 Supervisor 和 Worker 状态。"""
        sup_port = _find_free_port()
        sup = Supervisor(listen_addresses=[("127.0.0.1", sup_port)], worker_args=[])

        try:
            server = await asyncio.start_server(
                sup._handle_connection, host="127.0.0.1", port=sup_port,
            )
            sup._servers.append(server)

            # 设置 mock Worker
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.pid = 12345
            worker = WorkerProcess(port=9999, proc=mock_proc, generation=1)
            worker.ready = True
            sup._active_worker = worker

            status, body = await _http_get(sup_port, "/health")
            assert status == 200
            data = json.loads(body)
            assert data["status"] == "ok"
            assert data["mode"] == "supervisor"
            assert data["worker"]["status"] == "ready"
            assert data["worker"]["generation"] == 1

        finally:
            for s in sup._servers:
                s.close()
                await s.wait_closed()

    @pytest.mark.asyncio
    async def test_health_no_worker(self):
        """/health Worker 不存在时返回 worker.status=none。"""
        sup_port = _find_free_port()
        sup = Supervisor(listen_addresses=[("127.0.0.1", sup_port)], worker_args=[])

        try:
            server = await asyncio.start_server(
                sup._handle_connection, host="127.0.0.1", port=sup_port,
            )
            sup._servers.append(server)
            sup._active_worker = None

            status, body = await _http_get(sup_port, "/health")
            assert status == 200
            data = json.loads(body)
            assert data["worker"]["status"] == "none"

        finally:
            for s in sup._servers:
                s.close()
                await s.wait_closed()

    @pytest.mark.asyncio
    async def test_restart_endpoint(self):
        """POST /api/restart 等新 Worker ready 后返回 200,响应含 new_worker/old_worker。"""
        sup_port = _find_free_port()
        sup = Supervisor(listen_addresses=[("127.0.0.1", sup_port)], worker_args=[])
        sup._restarting = False

        # Mock _spawn_worker 避免真正 spawn 子进程
        spawn_called = False

        async def mock_spawn():
            nonlocal spawn_called
            spawn_called = True
            sup._generation += 1
            # 构造假 WorkerProcess
            import subprocess as _sp
            fake = WorkerProcess(port=9999, proc=_sp.Popen(["python", "-c", "import time; time.sleep(60)"]),
                                 generation=sup._generation)
            fake.ready = True
            sup._active_worker = fake
            return fake

        sup._spawn_worker = mock_spawn  # type: ignore

        # mock drain 不做任何事
        async def mock_drain_and_reap(old):
            sup._restarting = False
        sup._drain_and_reap = mock_drain_and_reap  # type: ignore

        try:
            server = await asyncio.start_server(
                sup._handle_connection, host="127.0.0.1", port=sup_port,
            )
            sup._servers.append(server)

            status, body = await _http_post(sup_port, "/api/restart")
            assert status == 200
            data = json.loads(body)
            assert data["status"] == "ok"
            assert "new_worker" in data
            assert data["new_worker"]["generation"] >= 1
            assert "supervisor" in data
            assert spawn_called

        finally:
            if sup._active_worker:
                sup._active_worker.kill()
            for s in sup._servers:
                s.close()
                await s.wait_closed()

    @pytest.mark.asyncio
    async def test_restart_endpoint_already_restarting(self):
        """重启进行中时返回 409。"""
        sup_port = _find_free_port()
        sup = Supervisor(listen_addresses=[("127.0.0.1", sup_port)], worker_args=[])
        sup._restarting = True  # 模拟重启中

        try:
            server = await asyncio.start_server(
                sup._handle_connection, host="127.0.0.1", port=sup_port,
            )
            sup._servers.append(server)

            status, body = await _http_post(sup_port, "/api/restart")
            assert status == 409
            data = json.loads(body)
            assert data["status"] == "already_restarting"

        finally:
            for s in sup._servers:
                s.close()
                await s.wait_closed()

    @pytest.mark.asyncio
    async def test_restart_get_not_allowed(self):
        """GET /api/restart 返回 405。"""
        sup_port = _find_free_port()
        sup = Supervisor(listen_addresses=[("127.0.0.1", sup_port)], worker_args=[])

        try:
            server = await asyncio.start_server(
                sup._handle_connection, host="127.0.0.1", port=sup_port,
            )
            sup._servers.append(server)

            status, body = await _http_get(sup_port, "/api/restart")
            assert status == 405

        finally:
            for s in sup._servers:
                s.close()
                await s.wait_closed()

    @pytest.mark.asyncio
    async def test_drain_worker(self):
        """drain_worker 调用 Worker 的 /internal/drain 端点。"""
        worker_port = _find_free_port()
        echo_server = await _start_echo_server(worker_port)

        sup = Supervisor(listen_addresses=[], worker_args=[])

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        worker = WorkerProcess(port=worker_port, proc=mock_proc, generation=1)

        try:
            result = await sup._drain_worker(worker)
            assert result is True
        finally:
            echo_server.close()
            await echo_server.wait_closed()

    @pytest.mark.asyncio
    async def test_drain_worker_connection_refused(self):
        """drain_worker Worker 不可达时返回 False。"""
        sup = Supervisor(listen_addresses=[], worker_args=[])

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        # 用一个不存在的端口
        worker = WorkerProcess(port=_find_free_port(), proc=mock_proc, generation=1)

        result = await sup._drain_worker(worker)
        assert result is False

    @pytest.mark.asyncio
    async def test_connection_count_tracking(self):
        """代理连接计数正确增减。"""
        worker_port = _find_free_port()
        sup_port = _find_free_port()

        echo_server = await _start_echo_server(worker_port)
        sup = Supervisor(listen_addresses=[("127.0.0.1", sup_port)], worker_args=[])

        try:
            server = await asyncio.start_server(
                sup._handle_connection, host="127.0.0.1", port=sup_port,
            )
            sup._servers.append(server)

            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.pid = 12345
            worker = WorkerProcess(port=worker_port, proc=mock_proc, generation=1)
            worker.ready = True
            sup._active_worker = worker

            assert worker.active_connections == 0

            # 发送请求（Connection: close 会让连接断开，计数回到 0）
            status, body = await _http_get(sup_port, "/test")
            assert status == 200

            # 等待连接关闭
            await asyncio.sleep(0.1)
            assert worker.active_connections == 0

        finally:
            for s in sup._servers:
                s.close()
                await s.wait_closed()
            echo_server.close()
            await echo_server.wait_closed()


# ---------------------------------------------------------------------------
# CLI entry point 测试
# ---------------------------------------------------------------------------

class TestCLIArgs:
    """CLI 参数解析测试。"""

    def test_parse_args_default(self):
        """默认参数：无 --worker，无 --no-supervisor。"""
        from mutbot.web.server import _parse_args
        import sys
        old_argv = sys.argv
        sys.argv = ["mutbot"]
        try:
            args = _parse_args()
            assert not args.worker
            assert not args.no_supervisor
            assert args.port is None
            assert not args.debug
        finally:
            sys.argv = old_argv

    def test_parse_args_worker_mode(self):
        """--worker --port 9000"""
        from mutbot.web.server import _parse_args
        import sys
        old_argv = sys.argv
        sys.argv = ["mutbot", "--worker", "--port", "9000"]
        try:
            args = _parse_args()
            assert args.worker
            assert args.port == 9000
        finally:
            sys.argv = old_argv

    def test_parse_args_no_supervisor(self):
        """--no-supervisor"""
        from mutbot.web.server import _parse_args
        import sys
        old_argv = sys.argv
        sys.argv = ["mutbot", "--no-supervisor"]
        try:
            args = _parse_args()
            assert args.no_supervisor
            assert not args.worker
        finally:
            sys.argv = old_argv

    def test_parse_args_debug(self):
        """--debug"""
        from mutbot.web.server import _parse_args
        import sys
        old_argv = sys.argv
        sys.argv = ["mutbot", "--debug"]
        try:
            args = _parse_args()
            assert args.debug
        finally:
            sys.argv = old_argv


# ---------------------------------------------------------------------------
# server.py 辅助函数测试
# ---------------------------------------------------------------------------

class TestServerHelpers:
    """server.py 中的辅助函数测试。"""

    def test_parse_listen_host_port(self):
        from mutbot.web.server import _parse_listen
        assert _parse_listen("127.0.0.1:8741") == ("127.0.0.1", 8741)
        assert _parse_listen("0.0.0.0:9000") == ("0.0.0.0", 9000)

    def test_parse_listen_port_only(self):
        from mutbot.web.server import _parse_listen
        assert _parse_listen("8741") == ("127.0.0.1", 8741)

    def test_collect_listen_addresses_dedup(self):
        from mutbot.web.server import _collect_listen_addresses
        result = _collect_listen_addresses(
            ["127.0.0.1:8741", "0.0.0.0:9000"],
            ["127.0.0.1:8741"],  # 重复
        )
        assert len(result) == 2
        assert ("127.0.0.1", 8741) in result
        assert ("0.0.0.0", 9000) in result

    def test_collect_listen_addresses_default(self):
        from mutbot.web.server import _collect_listen_addresses
        result = _collect_listen_addresses([], [])
        assert result == [("127.0.0.1", 8741)]
