"""API routes — REST endpoints and WebSocket handler."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mutbot.web.connection import ConnectionManager

logger = logging.getLogger(__name__)

router = APIRouter()
connection_manager = ConnectionManager()


def _get_managers():
    """Lazy import of global managers from server module."""
    from mutbot.web.server import workspace_manager, session_manager
    return workspace_manager, session_manager


def _workspace_dict(ws) -> dict[str, Any]:
    return {
        "id": ws.id,
        "name": ws.name,
        "project_path": ws.project_path,
        "sessions": ws.sessions,
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
            elif msg_type == "stop":
                sm.stop(session_id)
                break
    except WebSocketDisconnect:
        logger.info("WS disconnected: session=%s", session_id)
    except Exception:
        logger.exception("WS error: session=%s", session_id)
    finally:
        connection_manager.disconnect(session_id, websocket)
