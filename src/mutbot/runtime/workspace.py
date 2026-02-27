"""Workspace manager — workspace storage with file-based persistence."""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mutbot.runtime import storage

logger = logging.getLogger(__name__)


@dataclass
class Workspace:
    id: str
    name: str
    project_path: str
    sessions: list[str] = field(default_factory=list)
    layout: dict | None = None
    created_at: str = ""
    updated_at: str = ""
    last_accessed_at: str = ""


def _workspace_to_dict(ws: Workspace) -> dict:
    return {
        "id": ws.id,
        "name": ws.name,
        "project_path": ws.project_path,
        "sessions": ws.sessions,
        "layout": ws.layout,
        "created_at": ws.created_at,
        "updated_at": ws.updated_at,
        "last_accessed_at": ws.last_accessed_at,
    }


def _workspace_from_dict(d: dict) -> Workspace:
    return Workspace(
        id=d["id"],
        name=d.get("name", ""),
        project_path=d.get("project_path", "."),
        sessions=d.get("sessions", []),
        layout=d.get("layout"),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
        last_accessed_at=d.get("last_accessed_at", ""),
    )


def sanitize_workspace_name(name: str) -> str:
    """将名称转为 URL-safe slug（小写字母、数字、连字符）。"""
    slug = re.sub(r'[^a-z0-9-]', '-', name.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug or 'workspace'


class WorkspaceManager:
    """Workspace registry with file-based persistence."""

    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}

    def load_from_disk(self) -> None:
        """Load all workspaces from .mutbot/workspaces/*.json."""
        for data in storage.load_all_workspaces():
            ws = _workspace_from_dict(data)
            self._workspaces[ws.id] = ws
        if self._workspaces:
            logger.info("Loaded %d workspace(s) from disk", len(self._workspaces))

    def _persist(self, ws: Workspace) -> None:
        """Save workspace to disk."""
        storage.save_workspace(_workspace_to_dict(ws))

    def create(self, name: str, project_path: str) -> Workspace:
        slug = sanitize_workspace_name(name)
        # 确保名称唯一
        base = slug
        counter = 1
        while any(ws.name == slug for ws in self._workspaces.values()):
            slug = f"{base}-{counter}"
            counter += 1

        now = datetime.now(timezone.utc).isoformat()
        ws = Workspace(
            id=uuid.uuid4().hex[:12],
            name=slug,
            project_path=project_path,
            created_at=now,
            updated_at=now,
            last_accessed_at=now,
        )
        self._workspaces[ws.id] = ws
        self._persist(ws)
        return ws

    def get(self, workspace_id: str) -> Workspace | None:
        return self._workspaces.get(workspace_id)

    def get_by_name(self, name: str) -> Workspace | None:
        """按名称查找工作区。"""
        for ws in self._workspaces.values():
            if ws.name == name:
                return ws
        return None

    def list_all(self) -> list[Workspace]:
        """返回所有工作区，按 last_accessed_at 降序排列。"""
        wss = list(self._workspaces.values())
        wss.sort(key=lambda w: w.last_accessed_at or w.created_at, reverse=True)
        return wss

    def touch_accessed(self, ws: Workspace) -> None:
        """更新工作区的最后访问时间。"""
        ws.last_accessed_at = datetime.now(timezone.utc).isoformat()
        self._persist(ws)

    def update(self, ws: Workspace) -> None:
        """Persist workspace after mutation (e.g. layout change, session added)."""
        ws.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist(ws)

    def ensure_default(self) -> Workspace:
        """Auto-create a default workspace from cwd if none exist."""
        if self._workspaces:
            return next(iter(self._workspaces.values()))
        cwd = os.getcwd()
        name = os.path.basename(cwd) or "default"
        return self.create(name, cwd)
