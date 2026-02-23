"""MutBot Web server — FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from mutagent.runtime.log_store import LogStore, LogStoreHandler, SingleLineFormatter

from mutbot.workspace import WorkspaceManager
from mutbot.session import SessionManager
from mutbot.web.terminal import TerminalManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global managers (initialized at startup)
# ---------------------------------------------------------------------------

workspace_manager: WorkspaceManager | None = None
session_manager: SessionManager | None = None
log_store: LogStore | None = None
terminal_manager: TerminalManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global workspace_manager, session_manager, log_store, terminal_manager
    workspace_manager = WorkspaceManager()
    session_manager = SessionManager()
    terminal_manager = TerminalManager()
    workspace_manager.ensure_default()

    # --- Unified logging setup (mirrors mutagent pattern) ---
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path(".mutagent/logs")

    log_store = LogStore()

    # Capture both mutbot.* and mutagent.* loggers
    for root_name in ("mutbot", "mutagent"):
        root_logger = logging.getLogger(root_name)
        root_logger.setLevel(logging.DEBUG)

        # In-memory handler → LogStore (message only, timestamp in LogEntry)
        mem_handler = LogStoreHandler(log_store)
        mem_handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger.addHandler(mem_handler)

        # File handler → .mutagent/logs/YYYYMMDD_HHMMSS-log.log
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / f"{session_ts}-log.log", encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(SingleLineFormatter(
            "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
        ))
        root_logger.addHandler(file_handler)

    logger.info("Logging initialized (session=%s, log_dir=%s)", session_ts, log_dir)

    # Pass session_ts and log_dir to SessionManager for API recording
    session_manager.session_ts = session_ts
    session_manager.log_dir = log_dir

    yield

    # Shutdown: stop all active sessions and terminals
    if terminal_manager is not None:
        terminal_manager.kill_all()
    if session_manager is not None:
        for sid in list(session_manager._sessions):
            session_manager.stop(sid)


app = FastAPI(title="MutBot", version="0.0.1", lifespan=lifespan)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

from mutbot.web.routes import router as api_router  # noqa: E402
app.include_router(api_router)


# ---------------------------------------------------------------------------
# Static files (production build)
# ---------------------------------------------------------------------------

_frontend_dist = Path(__file__).resolve().parent.parent.parent.parent / "frontend_dist"
if _frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dist), html=True), name="static")
