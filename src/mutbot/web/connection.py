"""WebSocket connection manager."""

from __future__ import annotations

from fastapi import WebSocket


class ConnectionManager:
    """Tracks WebSocket connections per session."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(session_id, set()).add(websocket)

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

    def get_connections(self, session_id: str) -> set[WebSocket]:
        return self._connections.get(session_id, set())

    def has_connections(self, session_id: str) -> bool:
        return bool(self._connections.get(session_id))
