"""MutBot Web server — FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from mutbot.workspace import WorkspaceManager
from mutbot.session import SessionManager


# ---------------------------------------------------------------------------
# Global managers (initialized at startup)
# ---------------------------------------------------------------------------

workspace_manager: WorkspaceManager | None = None
session_manager: SessionManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global workspace_manager, session_manager
    workspace_manager = WorkspaceManager()
    session_manager = SessionManager()
    workspace_manager.ensure_default()
    yield
    # Shutdown: stop all active sessions
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
