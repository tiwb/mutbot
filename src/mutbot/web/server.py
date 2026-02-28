"""MutBot Web server — FastAPI application."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from mutagent.runtime.log_store import LogStore, LogStoreHandler, SingleLineFormatter

from mutbot.runtime.workspace import WorkspaceManager
from mutbot.runtime.session_impl import SessionManager
from mutbot.runtime.terminal import TerminalManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global managers (initialized at startup)
# ---------------------------------------------------------------------------

workspace_manager: WorkspaceManager | None = None
session_manager: SessionManager | None = None
log_store: LogStore | None = None
terminal_manager: TerminalManager | None = None


# ---------------------------------------------------------------------------
# Shutdown helpers
# ---------------------------------------------------------------------------

_SHUTDOWN_TIMEOUT = 10  # seconds

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


def _start_exit_watchdog():
    """Start a daemon thread that forces os._exit after SHUTDOWN_TIMEOUT seconds.

    Covers the case where uvicorn's own shutdown hangs before reaching
    the lifespan shutdown code.  Being a daemon thread, it is harmless
    if the process exits normally before the timeout.
    """
    import time
    import threading as _threading

    def _watchdog():
        time.sleep(_SHUTDOWN_TIMEOUT)
        print(f"\nShutdown timed out after {_SHUTDOWN_TIMEOUT}s, forcing exit...",
              flush=True)
        _force_exit_flush()
        os._exit(0)

    t = _threading.Thread(target=_watchdog, daemon=True, name="exit-watchdog")
    t.start()


_sigint_count = 0


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
        # 第一次 Ctrl+C：启动超时 watchdog，提示用户，然后交给 uvicorn 优雅退出
        _start_exit_watchdog()
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
    """Stop sessions that have active runtimes."""
    if terminal_manager is not None:
        terminal_manager.kill_all()
    if session_manager is not None:
        for sid in list(session_manager._runtimes):
            await session_manager.stop(sid)


def _ensure_setup_session(ws, sm, wm) -> None:
    """确保 setup 模式下 workspace 有一个可用的 Guide session。

    - 首次启动：创建 GuideSession，带 initial_message 触发欢迎
    - 重启恢复：已有 GuideSession 时，重新注入 initial_message
    - 通过 pending event 让前端连接后自动打开 Guide tab
    """
    from mutbot.web.routes import workspace_connection_manager

    guide_type = "mutbot.builtins.guide.GuideSession"
    existing = sm.list_by_workspace(ws.id)
    guide = next(
        (s for s in existing
         if s.type == guide_type and s.status == "active"),
        None,
    )

    if guide is None:
        guide = sm.create(
            ws.id,
            session_type=guide_type,
            config={"initial_message": "__setup__"},
        )
        ws.sessions.append(guide.id)
        wm.update(ws)
        logger.info("Setup mode: created Guide session %s", guide.id)
    elif "initial_message" not in guide.config:
        # 重启恢复：上次 initial_message 已消费，重新注入
        guide.config["initial_message"] = "__setup__"
        sm._persist(guide)
        logger.info("Setup mode: re-injected initial_message for session %s", guide.id)

    # 入队 open_session 事件，前端 WebSocket 连接后自动 flush
    workspace_connection_manager.queue_event(
        ws.id, "open_session", {"session_id": guide.id},
    )


async def _watch_config_changes() -> None:
    """Background task: poll ~/.mutbot/config.json mtime every 5s.

    On change, reload the SessionManager's cached config and broadcast
    a ``config_changed`` event to all workspace WebSocket clients.
    """
    from mutbot.runtime.config import MUTBOT_USER_DIR, load_mutbot_config
    from mutbot.web.routes import workspace_connection_manager
    from mutbot.web.rpc import make_event

    config_path = MUTBOT_USER_DIR / "config.json"
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
            # Invalidate SessionManager's cached config
            if session_manager is not None:
                session_manager._config = None
            await workspace_connection_manager.broadcast_all(
                make_event("config_changed", {"reason": "file_changed"})
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global workspace_manager, session_manager, log_store, terminal_manager
    workspace_manager = WorkspaceManager()
    session_manager = SessionManager()
    terminal_manager = TerminalManager()

    # --- Load persisted state ---
    workspace_manager.load_from_disk()

    # 收集所有 workspace 引用的 session ID，按需加载
    all_session_ids: set[str] = set()
    for ws in workspace_manager._workspaces.values():
        all_session_ids.update(ws.sessions)
    session_manager.load_from_disk(all_session_ids)

    # 服务器重启：所有旧 session 标记为 ended（无运行中的 agent）
    _ended = 0
    for session in session_manager._sessions.values():
        if session.status == "active":
            session.status = "ended"
            session_manager._persist(session)
            _ended += 1
    if _ended:
        logger.info("Marked %d stale session(s) as ended on restart", _ended)

    ws = workspace_manager.ensure_default()

    # --- Setup 模式：无 LLM 配置时自动创建向导 session ---
    from mutbot.runtime.config import load_mutbot_config
    _mutbot_config = load_mutbot_config()
    if not _mutbot_config.get("providers"):
        _ensure_setup_session(ws, session_manager, workspace_manager)

    # --- Unified logging setup ---
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path.home() / ".mutbot" / "logs"

    log_store = LogStore()

    # Capture both mutbot.* and mutagent.* loggers
    for root_name in ("mutbot", "mutagent"):
        root_logger = logging.getLogger(root_name)
        root_logger.setLevel(logging.DEBUG)

        # In-memory handler → LogStore (message only, timestamp in LogEntry)
        mem_handler = LogStoreHandler(log_store)
        mem_handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger.addHandler(mem_handler)

        # File handler → ~/.mutbot/logs/server-YYYYMMDD_HHMMSS.log
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / f"server-{session_ts}.log", encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(SingleLineFormatter(
            "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
        ))
        root_logger.addHandler(file_handler)

    logger.info("Logging initialized (session=%s, log_dir=%s)", session_ts, log_dir)

    # Pass log_dir to SessionManager for per-session API recording
    session_manager.log_dir = log_dir
    # Wire terminal_manager for Terminal Session lifecycle
    session_manager.terminal_manager = terminal_manager

    # --- Double Ctrl+C handler ---
    # Install AFTER uvicorn has set up its own SIGINT handler, so we can chain.
    # Lifespan startup runs during Server.startup(), after uvicorn's handler.
    _install_double_ctrlc_handler()

    # --- LLM proxy routes (optional, loaded via modules config) ---
    # 注意：proxy config 在 lifespan 中加载，但 router 在模块级注册（见文件末尾）
    try:
        from mutbot.proxy.routes import _providers_config as _pc
        from mutbot.runtime.session_impl import _load_config
        proxy_config = _load_config()
        if proxy_config is not None:
            from mutbot.proxy.routes import _providers_config
            import mutbot.proxy.routes as _proxy_routes
            _proxy_routes._providers_config = proxy_config.get("providers", {})
            logger.info("LLM proxy config loaded (%d providers)",
                        len(_proxy_routes._providers_config))
    except Exception:
        logger.warning("Failed to load LLM proxy config", exc_info=True)

    # --- Config file change watcher ---
    config_watcher_task = asyncio.create_task(_watch_config_changes())

    yield

    # --- Cancel config watcher ---
    config_watcher_task.cancel()
    try:
        await config_watcher_task
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


app = FastAPI(title="MutBot", version="0.1.0", lifespan=lifespan)


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
# Static files (production build)
# ---------------------------------------------------------------------------

_frontend_dist = Path(__file__).resolve().parent / "frontend_dist"
if _frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dist), html=True), name="static")
