"""WebSocket connection manager."""

from __future__ import annotations

import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks WebSocket connections per session/workspace.

    Supports a per-key pending event queue: events queued when no client is
    connected are automatically flushed to the first client that connects.
    """

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._pending_events: dict[str, list[dict]] = {}

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(session_id, set()).add(websocket)
        # Flush pending events to the newly connected client
        pending = self._pending_events.pop(session_id, None)
        if pending:
            logger.info("Flushing %d pending event(s) to %s", len(pending), session_id)
            for event in pending:
                try:
                    await websocket.send_json(event)
                except Exception:
                    break

    def disconnect(self, session_id: str, websocket: WebSocket) -> None:
        conns = self._connections.get(session_id)
        if conns:
            conns.discard(websocket)
            if not conns:
                del self._connections[session_id]

    async def broadcast(
        self,
        session_id: str,
        data: dict,
        exclude: WebSocket | None = None,
    ) -> None:
        conns = self._connections.get(session_id)
        if not conns:
            return
        dead = []
        for ws in list(conns):
            if ws is exclude:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            conns.discard(ws)

    def queue_event(self, key: str, event: str, data: dict | None = None) -> None:
        """Queue an event for delivery.

        If clients are connected the event is NOT sent immediately (use
        ``broadcast`` for that).  The event is stored and flushed when the
        next client connects via ``connect()``.
        """
        msg = {"type": "event", "event": event, "data": data or {}}
        self._pending_events.setdefault(key, []).append(msg)

    def get_connections(self, session_id: str) -> set[WebSocket]:
        return self._connections.get(session_id, set())

    def has_connections(self, session_id: str) -> bool:
        return bool(self._connections.get(session_id))

    async def broadcast_all(self, data: dict) -> None:
        """Broadcast to ALL connected WebSocket clients across all keys."""
        dead_pairs: list[tuple[str, WebSocket]] = []
        for key, conns in self._connections.items():
            for ws in list(conns):
                try:
                    await ws.send_json(data)
                except Exception:
                    dead_pairs.append((key, ws))
        for key, ws in dead_pairs:
            conns = self._connections.get(key)
            if conns:
                conns.discard(ws)
