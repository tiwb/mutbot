"""MutBot Web server — FastAPI application."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket as _socket
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from mutagent.runtime.log_store import LogStore, LogStoreHandler, SingleLineFormatter

from mutbot.runtime.workspace import WorkspaceManager
from mutbot.runtime.session_manager import SessionManager
from mutbot.runtime.terminal import TerminalManager
from mutbot.runtime.config import MutbotConfig, load_mutbot_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global managers (initialized at startup)
# ---------------------------------------------------------------------------

workspace_manager: WorkspaceManager | None = None
session_manager: SessionManager | None = None
log_store: LogStore | None = None
terminal_manager: TerminalManager | None = None
channel_manager: Any = None


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


_sigint_count = 0


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


def _install_double_ctrlc_handler():
    """Install a chained SIGINT handler: 1st Ctrl+C → uvicorn graceful, 2nd → os._exit.

    Must be called AFTER uvicorn has installed its own handler (i.e. during
    lifespan startup), so we can capture and chain uvicorn's handler.
    """
    global _sigint_count
    _sigint_count = 0

    try:
        prev_handler = signal.getsignal(signal.SIGINT)
    except (OSError, ValueError):
        return

    def _chained_sigint(signum, frame):
        global _sigint_count
        _sigint_count += 1
        if _sigint_count >= 2:
            print("\nForce shutting down...", flush=True)
            _force_exit_flush()
            os._exit(0)
        # 第一次 Ctrl+C：交给 uvicorn 优雅退出。
        # uvicorn 的 timeout_graceful_shutdown 会在超时后进入 lifespan exit，
        # 我们的 _shutdown_cleanup() 在 lifespan exit 中处理 session/client 清理。
        print("\nShutting down gracefully... Press Ctrl+C again to force exit",
              flush=True)
        if callable(prev_handler):
            prev_handler(signum, frame)

    try:
        signal.signal(signal.SIGINT, _chained_sigint)
        logger.info("Double Ctrl+C handler installed (chained with %s)", prev_handler)
    except (OSError, ValueError) as exc:
        logger.warning("Cannot install SIGINT handler: %s", exc)


async def _shutdown_cleanup():
    """Stop all sessions cleanly (saves scrollback, cleans up PTYs), then kill orphan terminals."""
    # Stop ALL sessions: on_stop() saves TerminalSession scrollback and kills their PTYs;
    # for AgentSessions it stops the bridge. _runtimes only holds AgentSessions so we
    # must iterate _sessions to reach TerminalSessions.
    if session_manager is not None:
        sids = list(session_manager._sessions)
        logger.info("Stopping %d sessions: %s", len(sids), sids)
        for sid in sids:
            await session_manager.stop(sid)

    # Stop all WebSocket clients — cancel _send_worker tasks that would otherwise
    # block the event loop and prevent uvicorn from shutting down.
    _stop_all_clients()

    # Kill any remaining terminals not owned by a session (safety net)
    if terminal_manager is not None:
        terminal_manager.kill_all()


async def _watch_config_changes(config: MutbotConfig) -> None:
    """Background task: poll ~/.mutbot/config.json mtime every 5s.

    On change, call config.reload() which auto-triggers on_change callbacks.
    """
    config_path = config._config_path
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
            config.reload()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global workspace_manager, session_manager, log_store, terminal_manager, channel_manager

    # --- Config & logging from app.state (initialized in main()) ---
    config = app.state.config
    log_store = app.state.log_store

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

    # 服务器重启：清除残留的运行时状态（各 Session 子类自行处理状态归位）
    _cleared = 0
    for session in session_manager._sessions.values():
        old_status = session.status
        session.on_restart_cleanup()
        if session.status != old_status:
            session_manager._persist(session)
            _cleared += 1
    if _cleared:
        logger.info("Cleared %d stale 'running' session(s) on restart", _cleared)

    # --- Setup 模式：推迟到 workspace.create RPC 中处理 ---

    log_dir = Path.home() / ".mutbot" / "logs"
    logger.info("Logging initialized (log_dir=%s)", log_dir)

    # Pass log_dir to SessionManager for per-session API recording
    session_manager.log_dir = log_dir
    # Wire terminal_manager for Terminal Session lifecycle
    session_manager.terminal_manager = terminal_manager

    # --- Double Ctrl+C handler ---
    # Install AFTER uvicorn has set up its own SIGINT handler, so we can chain.
    # Lifespan startup runs during Server.startup(), after uvicorn's handler.
    _install_double_ctrlc_handler()

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
    config_watcher_task = asyncio.create_task(_watch_config_changes(config))

    # --- Dirty session persist loop ---
    persist_dirty_task = asyncio.create_task(session_manager.persist_dirty_loop())

    # --- asyncio 未捕获异常兜底 ---
    def _asyncio_exception_handler(loop, context):
        exception = context.get("exception")
        message = context.get("message", "Unhandled exception in async task")
        _logger = logging.getLogger("mutbot.asyncio")
        if exception:
            # CPython bug: Windows Proactor 在 _call_connection_lost 中对已重置的
            # socket 调用 shutdown() 抛出 ConnectionResetError（WinError 10054）。
            # 此时 protocol.connection_lost() 已执行完毕，应用层断开处理不受影响。
            # https://github.com/python/cpython/issues/93821
            if isinstance(exception, ConnectionResetError):
                _logger.debug("%s: %s", message, exception)
            else:
                _logger.error("%s: %s", message, exception, exc_info=exception)
        else:
            _logger.error(message)

    asyncio.get_running_loop().set_exception_handler(_asyncio_exception_handler)

    yield

    # --- Cancel config watcher ---
    config_watcher_task.cancel()
    persist_dirty_task.cancel()
    try:
        await config_watcher_task
    except asyncio.CancelledError:
        pass
    try:
        await persist_dirty_task
    except asyncio.CancelledError:
        pass

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


import mutbot
app = FastAPI(title="MutBot", version=mutbot.__version__, lifespan=lifespan)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

from mutbot.web.routes import router as api_router  # noqa: E402
app.include_router(api_router)


# ---------------------------------------------------------------------------
# LLM proxy routes (module-level registration, config loaded in lifespan)
# ---------------------------------------------------------------------------

try:
    from mutbot.proxy import create_llm_router
    _llm_router = create_llm_router({})  # config 在 lifespan 中填充
    app.include_router(_llm_router, prefix="/llm")
except ImportError:
    pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8741


def _parse_listen(value: str) -> tuple[str, int]:
    """Parse a --listen / config listen value into (host, port).

    Formats: 'host:port', pure digits → port, otherwise → host.
    """
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
        ips = list(dict.fromkeys(info[4][0] for info in infos))
        # Ensure 127.0.0.1 is included
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
            # Expand to all local IPs
            for ip in _enumerate_ips():
                if (ip, port) in {(h, p) for h, p in addresses if h != "0.0.0.0"}:
                    continue  # Already covered by an explicit bind
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


def main():
    """MutBot server entry point: config → logging → app.state → uvicorn."""
    import argparse

    import uvicorn

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
    args = parser.parse_args()

    # 1. Config（进程最早阶段）
    config = load_mutbot_config()

    # 2. 日志初始化（紧随 config，保证不丢日志）
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path.home() / ".mutbot" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 控制台 StreamHandler（--debug 覆盖 config）
    if args.debug:
        console_level = logging.DEBUG
    else:
        level_name = config.get("logging.console_level", default="WARNING")
        console_level = getattr(logging, level_name.upper(), logging.WARNING)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(
        "%(levelname)-8s %(name)s: %(message)s",
    ))
    root_logger.addHandler(console_handler)

    # FileHandler → ~/.mutbot/logs/server-YYYYMMDD_HHMMSS.log（全量 DEBUG）
    file_handler = logging.FileHandler(
        log_dir / f"server-{session_ts}.log", encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(SingleLineFormatter(
        "%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    ))
    root_logger.addHandler(file_handler)

    # LogStoreHandler（全量 DEBUG，lifespan 中取出使用）
    _log_store = LogStore()
    mem_handler = LogStoreHandler(_log_store)
    mem_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(mem_handler)

    # 3. app.state 赋值
    app.state.config = config
    app.state.log_store = _log_store

    # 4. Listen 地址合并（CLI + config）
    cli_listen = args.listen or []
    config_listen = config.get("listen", default=[]) or []
    addresses = _collect_listen_addresses(cli_listen, config_listen)

    # 5. 创建 sockets
    sockets: list[_socket.socket] = []
    for host, port in addresses:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sockets.append(sock)

    # 6. uvicorn（log_config=None 禁用 uvicorn 自带日志配置）
    uvi_config = uvicorn.Config(
        app, log_config=None,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(uvi_config)

    # 7. Banner（防重复打印）
    _banner_printed = False
    _original_startup = server.startup

    async def _startup_with_banner(sockets=None):
        nonlocal _banner_printed
        await _original_startup(sockets=sockets)
        if not _banner_printed:
            _banner_printed = True
            banner_lines = _build_banner_lines(addresses)
            print(f"\n  MutBot v{mutbot.__version__}\n")
            for line in banner_lines:
                print(line)
            print()

    server.startup = _startup_with_banner

    try:
        server.run(sockets=sockets)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Static files (production build)
# ---------------------------------------------------------------------------

_frontend_dist = Path(__file__).resolve().parent / "frontend_dist"
if _frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dist), html=True), name="static")
