"""MutBot Web server — FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from mutagent.runtime.log_store import LogStore, LogStoreHandler, SingleLineFormatter

from mutbot.workspace import WorkspaceManager
from mutbot.session import SessionManager
from mutbot.web.terminal import TerminalManager
from mutbot.web.auth import AuthManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global managers (initialized at startup)
# ---------------------------------------------------------------------------

workspace_manager: WorkspaceManager | None = None
session_manager: SessionManager | None = None
log_store: LogStore | None = None
terminal_manager: TerminalManager | None = None
auth_manager: AuthManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global workspace_manager, session_manager, log_store, terminal_manager, auth_manager
    workspace_manager = WorkspaceManager()
    session_manager = SessionManager()
    terminal_manager = TerminalManager()

    # --- Auth setup ---
    auth_manager = AuthManager()
    auth_manager.load_config()

    # --- Load persisted state ---
    workspace_manager.load_from_disk()
    session_manager.load_from_disk()

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


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

# Paths that never require authentication
_AUTH_SKIP_PATHS = frozenset({
    "/api/auth/login",
    "/api/auth/status",
    "/api/health",
})

_AUTH_SKIP_PREFIXES = ("/docs", "/openapi", "/redoc")


class AuthMiddleware(BaseHTTPMiddleware):
    """Check Bearer token on /api/* and /ws/* paths."""

    async def dispatch(self, request: Request, call_next):
        am = auth_manager
        if am is None or not am.enabled:
            return await call_next(request)

        path = request.url.path

        # Skip auth for whitelisted paths
        if path in _AUTH_SKIP_PATHS or path.startswith(_AUTH_SKIP_PREFIXES):
            return await call_next(request)

        # Skip auth for static files (non-API, non-WS)
        if not path.startswith("/api/") and not path.startswith("/ws/"):
            return await call_next(request)

        # Skip for localhost when configured
        client_host = request.client.host if request.client else ""
        if am.should_skip_auth(client_host):
            return await call_next(request)

        # Check token
        token = None
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        if not token:
            # Also check query param (for WebSocket connections)
            token = request.query_params.get("token")

        if not token or am.verify_token(token) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        return await call_next(request)


app = FastAPI(title="MutBot", version="0.0.1", lifespan=lifespan)
app.add_middleware(AuthMiddleware)


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
