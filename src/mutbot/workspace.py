"""Workspace manager — in-memory workspace storage."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Workspace:
    id: str
    name: str
    project_path: str
    sessions: list[str] = field(default_factory=list)
    layout: dict | None = None
    created_at: str = ""
    updated_at: str = ""


class WorkspaceManager:
    """In-memory workspace registry."""

    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}

    def create(self, name: str, project_path: str) -> Workspace:
        now = datetime.now(timezone.utc).isoformat()
        ws = Workspace(
            id=uuid.uuid4().hex[:12],
            name=name,
            project_path=project_path,
            created_at=now,
            updated_at=now,
        )
        self._workspaces[ws.id] = ws
        return ws

    def get(self, workspace_id: str) -> Workspace | None:
        return self._workspaces.get(workspace_id)

    def list_all(self) -> list[Workspace]:
        return list(self._workspaces.values())

    def ensure_default(self) -> Workspace:
        """Auto-create a default workspace from cwd if none exist."""
        if self._workspaces:
            return next(iter(self._workspaces.values()))
        cwd = os.getcwd()
        name = os.path.basename(cwd) or "default"
        return self.create(name, cwd)
