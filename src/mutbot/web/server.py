"""MutBot Web server — MutBotServer + lifespan + 启动入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import socket as _socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from mutagent.runtime.log_store import LogStore, LogStoreHandler, SingleLineFormatter

from mutbot.runtime.workspace import WorkspaceManager
from mutbot.runtime.session_manager import SessionManager
from mutbot.runtime.terminal import TerminalManager
from mutbot.runtime.config import MutbotConfig, load_mutbot_config
from mutagent.net.server import Server, StaticView

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global managers (initialized at startup)
# ---------------------------------------------------------------------------

workspace_manager: WorkspaceManager | None = None
session_manager: SessionManager | None = None
log_store: LogStore | None = None
terminal_manager: TerminalManager | None = None
channel_manager: Any = None
config: MutbotConfig | None = None


# ---------------------------------------------------------------------------
# Shutdown helpers
# ---------------------------------------------------------------------------

def _force_exit_flush():
    """Flush standard streams and log handlers before os._exit()."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass



def _stop_all_clients():
    """Stop all WebSocket clients, cancelling their _send_worker tasks."""
    try:
        from mutbot.web.routes import _clients
        clients = list(_clients.values())
        if clients:
            logger.info("Stopping %d WebSocket client(s)", len(clients))
            for client in clients:
                client.stop()
    except Exception:
        pass


async def _shutdown_cleanup():
    """Stop non-terminal sessions, close ptyhost connection (terminals survive restart)."""
    if session_manager is not None:
        from mutbot.session import TerminalSession
        sids = list(session_manager._sessions)
        # 只停止非 Terminal 的 Session（Agent 等）；Terminal 留给 ptyhost 持久化
        non_terminal_sids = []
        terminal_sids = []
        for sid in sids:
            s = session_manager._sessions.get(sid)
            if s and isinstance(s, TerminalSession):
                terminal_sids.append(sid)
            else:
                non_terminal_sids.append(sid)

        if non_terminal_sids:
            logger.info("Stopping %d non-terminal sessions: %s", len(non_terminal_sids), non_terminal_sids)
            for sid in non_terminal_sids:
                await session_manager.stop(sid)

        if terminal_sids:
            logger.info("Preserving %d terminal sessions (ptyhost keeps PTYs alive)", len(terminal_sids))

    # Stop all WebSocket clients
    _stop_all_clients()

    # 关闭 ptyhost 连接
    if terminal_manager is not None:
        await terminal_manager.close()


async def _watch_config_changes(cfg: MutbotConfig) -> None:
    """Background task: poll ~/.mutbot/config.json mtime every 5s."""
    config_path = cfg._config_path
    last_mtime: float = 0.0
    try:
        last_mtime = config_path.stat().st_mtime
    except OSError:
        pass

    while True:
        await asyncio.sleep(5)
        try:
            current_mtime = config_path.stat().st_mtime
        except OSError:
            continue
        if current_mtime != last_mtime:
            last_mtime = current_mtime
            logger.info("Config file changed, reloading")
            cfg.reload()


# Background tasks（用于 on_shutdown 取消）
_background_tasks: list[asyncio.Task[Any]] = []


async def _on_startup() -> None:
    """MutBot 启动逻辑（对应 Server.on_startup）。"""
    global workspace_manager, session_manager, log_store, terminal_manager, channel_manager

    assert config is not None, "config must be set before startup"

    # import auth 模块，触发 View 子类和 @impl 注册
    import mutbot.auth.views as _auth_views  # noqa: F401
    import mutbot.auth.relay as _auth_relay  # noqa: F401
    import mutbot.auth.middleware as _auth_mw  # noqa: F401

    workspace_manager = WorkspaceManager()
    session_manager = SessionManager(config=config)
    terminal_manager = TerminalManager()

    from mutbot.web.transport import ChannelManager as _ChannelManager
    channel_manager = _ChannelManager()

    # --- Load persisted state ---
    workspace_manager.load_from_disk()

    # 收集所有 workspace 引用的 session ID，按需加载
    all_session_ids: set[str] = set()
    for ws in workspace_manager._workspaces.values():
        all_session_ids.update(ws.sessions)
    session_manager.load_from_disk(all_session_ids)

    # 服务器重启：非终端 Session 清状态；终端 Session 由 ptyhost 同步确认
    _cleared = 0
    for session in session_manager._sessions.values():
        old_status = session.status
        session.on_restart_cleanup()
        if session.status != old_status:
            session_manager._persist(session)
            _cleared += 1
    if _cleared:
        logger.info("Cleared %d stale 'running' session(s) on restart", _cleared)

    log_dir = Path.home() / ".mutbot" / "logs"
    logger.info("Logging initialized (log_dir=%s)", log_dir)

    # Pass log_dir to SessionManager for per-session API recording
    session_manager.log_dir = log_dir
    # Wire terminal_manager for Terminal Session lifecycle
    session_manager.terminal_manager = terminal_manager

    # --- on_change 回调统一注册 ---

    # 1. LLM client 重建（所有活跃 session）
    def _on_provider_changed(event):
        from mutbot.runtime.session_manager import create_llm_client, AgentSessionRuntime
        assert session_manager is not None
        for _sid, rt in session_manager._runtimes.items():
            if isinstance(rt, AgentSessionRuntime) and rt.agent:
                try:
                    rt.agent.llm = create_llm_client(event.config)
                except Exception:
                    logger.warning("Failed to rebuild LLM client for session %s", _sid, exc_info=True)

    config.on_change("providers.**", _on_provider_changed)
    config.on_change("default_model", _on_provider_changed)

    # 2. Proxy 配置刷新
    def _refresh_proxy(event):
        try:
            import mutbot.proxy.routes as _proxy_routes
            _proxy_routes._providers_config = event.config.get("providers", default={}) or {}
        except ImportError:
            pass

    config.on_change("providers.**", _refresh_proxy)

    # 3. WS 广播 config_changed
    def _broadcast_config_changed(event):
        from mutbot.web.routes import _broadcast_to_all_workspaces
        from mutbot.web.rpc import make_event
        _broadcast_to_all_workspaces(
            make_event("config_changed", {
                "reason": event.source or "changed",
                "key": event.key,
            })
        )

    config.on_change("**", _broadcast_config_changed)

    # --- LLM proxy 初始配置 ---
    try:
        import mutbot.proxy.routes as _proxy_routes
        _proxy_routes._providers_config = config.get("providers", default={}) or {}
        logger.info("LLM proxy config loaded (%d providers)",
                    len(_proxy_routes._providers_config))
    except Exception:
        logger.warning("Failed to load LLM proxy config", exc_info=True)

    # --- Config file change watcher ---
    _background_tasks.append(asyncio.create_task(_watch_config_changes(config)))

    # --- Dirty session persist loop ---
    _background_tasks.append(asyncio.create_task(session_manager.persist_dirty_loop()))

    # --- asyncio 未捕获异常兜底 ---
    def _asyncio_exception_handler(loop, context):
        exception = context.get("exception")
        message = context.get("message", "Unhandled exception in async task")
        _logger = logging.getLogger("mutbot.asyncio")
        if exception:
            if isinstance(exception, ConnectionResetError):
                _logger.debug("%s: %s", message, exception)
            else:
                _logger.error("%s: %s", message, exception, exc_info=exception)
        else:
            _logger.error(message)

    asyncio.get_running_loop().set_exception_handler(_asyncio_exception_handler)


async def _on_shutdown() -> None:
    """MutBot 关闭逻辑（对应 Server.on_shutdown）。"""
    # --- Cancel background tasks ---
    for task in _background_tasks:
        task.cancel()
    for task in _background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    _background_tasks.clear()

    # --- Graceful shutdown with timeout fallback ---
    import threading as _threading
    logger.info(
        "Shutdown started. Active threads: %s",
        [(t.name, t.daemon) for t in _threading.enumerate()],
    )
    try:
        await asyncio.wait_for(_shutdown_cleanup(), timeout=10.0)
        logger.info("Shutdown cleanup completed normally")
    except asyncio.TimeoutError:
        logger.warning("Shutdown cleanup timed out after 10s, forcing exit")
        _force_exit_flush()
        os._exit(0)
    except Exception:
        logger.exception("Shutdown cleanup raised unexpected error")
    logger.info(
        "Post-cleanup threads: %s",
        [(t.name, t.daemon) for t in _threading.enumerate()],
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8741


def _parse_listen(value: str) -> tuple[str, int]:
    """Parse a --listen / config listen value into (host, port)."""
    if ":" in value:
        host, port_str = value.rsplit(":", 1)
        return host, int(port_str)
    if value.isdigit():
        return _DEFAULT_HOST, int(value)
    return value, _DEFAULT_PORT


def _collect_listen_addresses(
    cli_listen: list[str],
    config_listen: list[str],
) -> list[tuple[str, int]]:
    """Merge CLI and config listen addresses, deduplicate."""
    seen: set[tuple[str, int]] = set()
    result: list[tuple[str, int]] = []
    for value in cli_listen + config_listen:
        addr = _parse_listen(value)
        if addr not in seen:
            seen.add(addr)
            result.append(addr)
    if not result:
        result.append((_DEFAULT_HOST, _DEFAULT_PORT))
    return result


def _enumerate_ips() -> list[str]:
    """Enumerate local IPv4 addresses (for 0.0.0.0 expansion)."""
    try:
        infos = _socket.getaddrinfo(
            _socket.gethostname(), None, _socket.AF_INET,
        )
        ips: list[str] = list(dict.fromkeys(str(info[4][0]) for info in infos))
        if "127.0.0.1" not in ips:
            ips.insert(0, "127.0.0.1")
        return ips
    except Exception:
        return ["127.0.0.1"]


def _build_banner_lines(
    addresses: list[tuple[str, int]],
) -> list[str]:
    """Build banner display lines with via URLs."""
    lines: list[str] = []
    for host, port in addresses:
        if host == "0.0.0.0":
            for ip in _enumerate_ips():
                if (ip, port) in {(h, p) for h, p in addresses if h != "0.0.0.0"}:
                    continue
                lines.append(_format_banner_line(ip, port))
        else:
            lines.append(_format_banner_line(host, port))
    return lines


def _format_banner_line(host: str, port: int) -> str:
    local_url = f"http://{host}:{port}"
    if host == "127.0.0.1" and port == _DEFAULT_PORT:
        via_url = "https://mutbot.ai"
    else:
        via_url = f"https://mutbot.ai/connect/#{host}:{port}"
    return f"  \u279c {local_url}  (via {via_url})"


def _init_logging(cfg: MutbotConfig, debug: bool, log_prefix: str = "server") -> LogStore:
    """初始化日志系统（config + console + file + memory store）。"""
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path.home() / ".mutbot" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 控制台 StreamHandler（--debug 覆盖 config）
    if debug:
        console_level = logging.DEBUG
    else:
        level_name = cfg.get("logging.console_level", default="WARNING")
        console_level = getattr(logging, level_name.upper(), logging.WARNING)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(
        "%(levelname)-8s %(name)s: %(message)s",
    ))
    root_logger.addHandler(console_handler)

    # FileHandler → ~/.mutbot/logs/<prefix>-YYYYMMDD_HHMMSS.log（全量 DEBUG）
    file_handler = logging.FileHandler(
        log_dir / f"{log_prefix}-{session_ts}.log", encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(SingleLineFormatter(
        "%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    ))
    root_logger.addHandler(file_handler)

    # LogStoreHandler（全量 DEBUG，on_startup 中取出使用）
    store = LogStore()
    mem_handler = LogStoreHandler(store)
    mem_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(mem_handler)

    return store


def _parse_args():
    """解析命令行参数。"""
    import argparse
    import mutbot

    parser = argparse.ArgumentParser(description="MutBot Web UI")
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"mutbot {mutbot.__version__}",
    )
    parser.add_argument(
        "--listen", action="append", default=None, metavar="[HOST:]PORT",
        help="Bind address (repeatable). Default: 127.0.0.1:8741",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to console")

    # Supervisor / Worker 模式
    parser.add_argument(
        "--worker", action="store_true",
        help="Run as Worker process (internal, used by Supervisor)",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Worker listen port (used with --worker)",
    )
    parser.add_argument(
        "--no-supervisor", action="store_true",
        help="Run in single-process mode (bypass Supervisor)",
    )

    return parser.parse_args()


def worker_main(port: int, debug: bool = False) -> None:
    """Worker 进程入口：监听 localhost 指定端口，运行完整 MutBotServer。"""
    import mutbot
    from mutagent.net.server import Server as _Server
    from mutagent import impl as _impl

    # 1. Config
    global config, log_store
    config = load_mutbot_config()

    # 2. 日志（Worker 用 server- 前缀，与单进程模式一致）
    log_store = _init_logging(config, debug, log_prefix="server")

    # 3. import 路由模块以触发 View/WebSocketView 子类注册
    import mutbot.web.routes  # noqa: F401
    import mutbot.web.mcp  # noqa: F401
    try:
        import mutbot.proxy.routes  # noqa: F401
    except ImportError:
        pass

    # 4. 静态文件
    _frontend_dist = Path(__file__).resolve().parent / "frontend_dist"
    if _frontend_dist.is_dir():
        class _FrontendStatic(StaticView):
            path = "/"
            directory = str(_frontend_dist)

    # 5. 创建 MutBotServer
    class MutBotServer(_Server):
        pass

    @_impl(MutBotServer.on_startup)
    async def _mutbot_on_startup(self):
        await _on_startup()
        logger.info("Worker ready on port %d (pid=%d)", port, os.getpid())

    @_impl(MutBotServer.on_shutdown)
    async def _mutbot_on_shutdown(self):
        await _on_shutdown()

    # 6. 创建 socket（Worker 只监听 localhost）
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))

    # 7. 启动
    _base_path = config.get("base_path", default="") or ""
    server = MutBotServer(base_path=_base_path)
    try:
        server.run(listen=[sock])
    except KeyboardInterrupt:
        pass


def supervisor_main(
    listen_addresses: list[tuple[str, int]],
    debug: bool = False,
    extra_worker_args: list[str] | None = None,
) -> None:
    """Supervisor 进程入口：启动 TCP 代理 + 管理 Worker 子进程。"""
    from mutbot.web.supervisor import Supervisor

    # Supervisor 自己的日志（不加载业务模块）
    cfg = load_mutbot_config()
    _init_logging(cfg, debug, log_prefix="supervisor")

    # 构造传给 Worker 的额外参数
    worker_args: list[str] = []
    if debug:
        worker_args.append("--debug")
    if extra_worker_args:
        worker_args.extend(extra_worker_args)

    supervisor = Supervisor(
        listen_addresses=listen_addresses,
        worker_args=worker_args,
        debug=debug,
        base_path=cfg.get("base_path", default="") or "",
    )
    supervisor.run()


def main():
    """MutBot server entry point — 根据参数选择 Supervisor / Worker / 单进程模式。"""
    import os
    from mutbot.runtime import storage
    storage.STARTUP_CWD = os.getcwd()

    args = _parse_args()

    # Worker 模式（由 Supervisor spawn）
    if args.worker:
        port = args.port or _DEFAULT_PORT
        worker_main(port=port, debug=args.debug)
        return

    # 解析监听地址
    global config
    cfg = load_mutbot_config()
    cli_listen = args.listen or []
    config_listen = cfg.get("listen", default=[]) or []
    addresses = _collect_listen_addresses(cli_listen, config_listen)

    # 单进程模式（--no-supervisor 或调试用）
    if args.no_supervisor:
        _standalone_main(addresses, args.debug)
        return

    # 默认：Supervisor 模式
    supervisor_main(listen_addresses=addresses, debug=args.debug)


def _standalone_main(addresses: list[tuple[str, int]], debug: bool = False) -> None:
    """单进程模式（与原来的 main() 行为一致）。"""
    import mutbot
    from mutagent.net.server import Server as _Server
    from mutagent import impl as _impl

    # 1. Config
    global config, log_store
    config = load_mutbot_config()

    # 2. 日志
    log_store = _init_logging(config, debug)

    # 3. import 路由模块
    import mutbot.web.routes  # noqa: F401
    import mutbot.web.mcp  # noqa: F401
    try:
        import mutbot.proxy.routes  # noqa: F401
    except ImportError:
        pass

    # 4. 静态文件
    _frontend_dist = Path(__file__).resolve().parent / "frontend_dist"
    if _frontend_dist.is_dir():
        class _FrontendStatic(StaticView):
            path = "/"
            directory = str(_frontend_dist)

    # 5. 创建 MutBotServer
    class MutBotServer(_Server):
        pass

    @_impl(MutBotServer.on_startup)
    async def _mutbot_on_startup(self):
        await _on_startup()
        banner_lines = _build_banner_lines(addresses)
        print(f"\n  MutBot v{mutbot.__version__}\n")
        for line in banner_lines:
            print(line)
        print()

    @_impl(MutBotServer.on_shutdown)
    async def _mutbot_on_shutdown(self):
        await _on_shutdown()

    # 6. 创建 sockets
    sockets: list[_socket.socket] = []
    for host, port in addresses:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sockets.append(sock)

    # 7. 启动
    _base_path = config.get("base_path", default="") or ""
    server = MutBotServer(base_path=_base_path)
    try:
        server.run(listen=sockets)
    except KeyboardInterrupt:
        pass
