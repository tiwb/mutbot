"""API routes — REST endpoints and WebSocket handler."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from mutbot.web.connection import ConnectionManager

logger = logging.getLogger(__name__)

router = APIRouter()
connection_manager = ConnectionManager()


def _get_managers():
    """Lazy import of global managers from server module."""
    from mutbot.web.server import workspace_manager, session_manager
    return workspace_manager, session_manager


def _get_log_store():
    from mutbot.web.server import log_store
    return log_store


def _get_terminal_manager():
    from mutbot.web.server import terminal_manager
    return terminal_manager


def _workspace_dict(ws) -> dict[str, Any]:
    return {
        "id": ws.id,
        "name": ws.name,
        "project_path": ws.project_path,
        "sessions": ws.sessions,
        "layout": ws.layout,
        "created_at": ws.created_at,
        "updated_at": ws.updated_at,
    }


def _session_dict(s) -> dict[str, Any]:
    return {
        "id": s.id,
        "workspace_id": s.workspace_id,
        "title": s.title,
        "status": s.status,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@router.get("/api/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Log query endpoint
# ---------------------------------------------------------------------------

@router.get("/api/logs")
async def query_logs(
    pattern: str = Query("", description="Regex pattern to match against message"),
    level: str = Query("DEBUG", description="Minimum log level (DEBUG/INFO/WARNING/ERROR)"),
    limit: int = Query(50, description="Maximum number of entries to return", ge=1, le=500),
):
    """Query in-memory log entries (newest first)."""
    store = _get_log_store()
    if store is None:
        return {"entries": [], "total": 0}
    entries = store.query(pattern=pattern, level=level, limit=limit)
    return {
        "total": store.count(),
        "returned": len(entries),
        "entries": [
            {
                "timestamp": e.timestamp,
                "level": e.level,
                "logger": e.logger_name,
                "message": e.message,
            }
            for e in entries
        ],
    }


# ---------------------------------------------------------------------------
# Workspace endpoints
# ---------------------------------------------------------------------------

@router.get("/api/workspaces")
async def list_workspaces():
    wm, _ = _get_managers()
    return [_workspace_dict(ws) for ws in wm.list_all()]


@router.post("/api/workspaces")
async def create_workspace(body: dict[str, Any]):
    wm, _ = _get_managers()
    name = body.get("name", "untitled")
    project_path = body.get("project_path", ".")
    ws = wm.create(name, project_path)
    return _workspace_dict(ws)


@router.get("/api/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str):
    wm, _ = _get_managers()
    ws = wm.get(workspace_id)
    if ws is None:
        return {"error": "workspace not found"}, 404
    return _workspace_dict(ws)


@router.put("/api/workspaces/{workspace_id}")
async def update_workspace(workspace_id: str, body: dict[str, Any]):
    wm, _ = _get_managers()
    ws = wm.get(workspace_id)
    if ws is None:
        return {"error": "workspace not found"}, 404
    if "layout" in body:
        ws.layout = body["layout"]
    return _workspace_dict(ws)


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------

@router.post("/api/workspaces/{workspace_id}/sessions")
async def create_session(workspace_id: str):
    wm, sm = _get_managers()
    ws = wm.get(workspace_id)
    if ws is None:
        return {"error": "workspace not found"}, 404
    session = sm.create(workspace_id)
    ws.sessions.append(session.id)
    return _session_dict(session)


@router.get("/api/workspaces/{workspace_id}/sessions")
async def list_sessions(workspace_id: str):
    _, sm = _get_managers()
    return [_session_dict(s) for s in sm.list_by_workspace(workspace_id)]


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    _, sm = _get_managers()
    session = sm.get(session_id)
    if session is None:
        return {"error": "session not found"}, 404
    return _session_dict(session)


@router.delete("/api/sessions/{session_id}")
async def stop_session(session_id: str):
    _, sm = _get_managers()
    sm.stop(session_id)
    return {"status": "stopped"}


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

fe_logger = logging.getLogger("mutbot.frontend")


@router.websocket("/ws/session/{session_id}")
async def websocket_session(websocket: WebSocket, session_id: str):
    _, sm = _get_managers()
    session = sm.get(session_id)
    if session is None:
        await websocket.close(code=4004, reason="session not found")
        return

    await connection_manager.connect(session_id, websocket)
    logger.info("WS connected: session=%s", session_id)

    # Start agent bridge if not running (forwarder is managed by the bridge)
    loop = asyncio.get_running_loop()
    try:
        bridge = sm.start(session_id, loop, connection_manager.broadcast)
    except Exception as exc:
        logger.exception("Failed to start agent for session=%s", session_id)
        await websocket.send_json({"type": "error", "error": str(exc)})
        connection_manager.disconnect(session_id, websocket)
        await websocket.close(code=4500, reason="agent start failed")
        return

    try:
        while True:
            raw = await websocket.receive_json()
            msg_type = raw.get("type", "")
            if msg_type == "message":
                text = raw.get("text", "")
                data = raw.get("data")
                if text:
                    bridge.send_message(text, data)
            elif msg_type == "log":
                # Frontend log forwarding
                level = raw.get("level", "debug")
                message = raw.get("message", "")
                log_fn = getattr(fe_logger, level, fe_logger.debug)
                log_fn("[%s] %s", session_id[:8], message)
            elif msg_type == "stop":
                sm.stop(session_id)
                break
    except WebSocketDisconnect:
        logger.info("WS disconnected: session=%s", session_id)
    except Exception:
        logger.exception("WS error: session=%s", session_id)
    finally:
        connection_manager.disconnect(session_id, websocket)


# ---------------------------------------------------------------------------
# Terminal endpoints
# ---------------------------------------------------------------------------

@router.post("/api/workspaces/{workspace_id}/terminals")
async def create_terminal_endpoint(workspace_id: str, body: dict[str, Any]):
    wm, _ = _get_managers()
    ws = wm.get(workspace_id)
    if ws is None:
        return JSONResponse({"error": "workspace not found"}, status_code=404)
    tm = _get_terminal_manager()
    if tm is None:
        return JSONResponse({"error": "terminal manager not available"}, status_code=503)
    rows = body.get("rows", 24)
    cols = body.get("cols", 80)
    term = tm.create(workspace_id, rows, cols, cwd=ws.project_path)
    return {"id": term.id, "workspace_id": term.workspace_id}


@router.websocket("/ws/terminal/{term_id}")
async def websocket_terminal(websocket: WebSocket, term_id: str):
    """Binary WebSocket for terminal I/O.

    Protocol:
    - Client→Server: 0x00 + input bytes
    - Server→Client: 0x01 + output bytes
    - Client→Server: 0x02 + 2B rows (big-endian) + 2B cols (big-endian)
    """
    tm = _get_terminal_manager()
    if tm is None or not tm.has(term_id):
        await websocket.close(code=4004, reason="terminal not found")
        return

    await websocket.accept()
    logger.info("Terminal WS connected: term=%s", term_id)

    loop = asyncio.get_running_loop()

    def on_output(data: bytes):
        """Called from reader thread when PTY produces output."""
        payload = b"\x01" + data
        asyncio.run_coroutine_threadsafe(
            websocket.send_bytes(payload), loop
        )

    tm.start_reader(term_id, loop, on_output)

    try:
        while True:
            raw = await websocket.receive_bytes()
            if len(raw) < 1:
                continue
            msg_type = raw[0]
            if msg_type == 0x00:
                # Terminal input
                tm.write(term_id, raw[1:])
            elif msg_type == 0x02 and len(raw) >= 5:
                # Resize: 2B rows + 2B cols (big-endian)
                rows = int.from_bytes(raw[1:3], "big")
                cols = int.from_bytes(raw[3:5], "big")
                tm.resize(term_id, rows, cols)
    except WebSocketDisconnect:
        logger.info("Terminal WS disconnected: term=%s", term_id)
    except Exception:
        logger.exception("Terminal WS error: term=%s", term_id)
    finally:
        tm.kill(term_id)


# ---------------------------------------------------------------------------
# Log streaming WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """Stream new log entries to client in real-time."""
    await websocket.accept()
    logger.info("Log WS connected")

    store = _get_log_store()
    if store is None:
        await websocket.close(code=4500, reason="log store not available")
        return

    # Start from current end (no history replay)
    cursor = store.count()

    try:
        while True:
            await asyncio.sleep(0.2)
            current_count = store.count()
            if current_count > cursor:
                # Fetch new entries
                new_entries = store.query(pattern="", level="DEBUG", limit=current_count - cursor)
                # query returns newest-first, reverse to send oldest-first
                for e in reversed(new_entries):
                    await websocket.send_json({
                        "type": "log",
                        "timestamp": e.timestamp,
                        "level": e.level,
                        "logger": e.logger_name,
                        "message": e.message,
                    })
                cursor = current_count
    except WebSocketDisconnect:
        logger.info("Log WS disconnected")
    except Exception:
        logger.exception("Log WS error")


# ---------------------------------------------------------------------------
# File read endpoint
# ---------------------------------------------------------------------------

_LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescriptreact",
    ".jsx": "javascriptreact", ".json": "json", ".html": "html", ".css": "css",
    ".md": "markdown", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".sh": "shell", ".bash": "shell", ".sql": "sql", ".xml": "xml",
    ".rs": "rust", ".go": "go", ".java": "java", ".c": "c", ".cpp": "cpp",
    ".h": "c", ".hpp": "cpp", ".rb": "ruby", ".php": "php",
}


@router.get("/api/workspaces/{workspace_id}/file")
async def read_file(workspace_id: str, path: str = Query(..., description="Relative file path")):
    wm, _ = _get_managers()
    ws = wm.get(workspace_id)
    if ws is None:
        return JSONResponse({"error": "workspace not found"}, status_code=404)

    # Resolve and verify path is within project
    project = Path(ws.project_path).resolve()
    target = (project / path).resolve()
    if not str(target).startswith(str(project)):
        return JSONResponse({"error": "path traversal not allowed"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    ext = target.suffix.lower()
    language = _LANG_MAP.get(ext, "plaintext")

    return {"path": str(target.relative_to(project)), "content": content, "language": language}
