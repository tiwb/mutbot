"""File-based persistence — JSON / JSONL with atomic writes."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default root for mutbot persistence (用户级，所有项目共享)
MUTBOT_DIR = str(Path.home() / ".mutbot")


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def save_json(path: Path, data: Any) -> None:
    """Atomic JSON write: write to temp file then os.replace."""
    _ensure_dir(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: Path) -> dict | None:
    """Load a JSON file, return None if missing or corrupt."""
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def _mutbot_path(*parts: str) -> Path:
    return Path(MUTBOT_DIR).joinpath(*parts)


def _find_session_file(session_id: str, suffix: str) -> Path | None:
    """按 session_id 查找文件，兼容新旧格式。

    新格式 ``{ts}-{session_id}{suffix}``，旧格式 ``{session_id}{suffix}``，
    glob ``*{session_id}{suffix}`` 均可匹配。
    """
    sess_dir = _mutbot_path("sessions")
    if not sess_dir.is_dir():
        return None
    matches = list(sess_dir.glob(f"*{session_id}{suffix}"))
    return matches[0] if matches else None


def _session_ts_prefix(created_at: str) -> str:
    """从 ISO UTC 时间戳构建本地时间前缀 ``YYYYMMDD_HHMMSS-``。"""
    dt_utc = datetime.fromisoformat(created_at)
    dt_local = dt_utc.astimezone()
    return dt_local.strftime("%Y%m%d_%H%M%S") + "-"


def save_workspace(ws_data: dict) -> None:
    path = _mutbot_path("workspaces", f"{ws_data['id']}.json")
    save_json(path, ws_data)


def load_all_workspaces() -> list[dict]:
    ws_dir = _mutbot_path("workspaces")
    if not ws_dir.is_dir():
        return []
    results = []
    for f in ws_dir.glob("*.json"):
        data = load_json(f)
        if data and "id" in data:
            results.append(data)
    return results


def save_session_metadata(session_data: dict) -> None:
    sid = session_data["id"]
    prefix = _session_ts_prefix(session_data.get("created_at", ""))
    path = _mutbot_path("sessions", f"{prefix}{sid}.json")
    save_json(path, session_data)


def load_session_metadata(session_id: str) -> dict | None:
    path = _find_session_file(session_id, ".json")
    if path is None:
        return None
    return load_json(path)


def load_all_sessions() -> list[dict]:
    sess_dir = _mutbot_path("sessions")
    if not sess_dir.is_dir():
        return []
    results = []
    for f in sess_dir.glob("*.json"):
        data = load_json(f)
        if data and "id" in data:
            results.append(data)
    return results


def load_sessions(session_ids: set[str]) -> list[dict]:
    """只加载指定 ID 集合中的 session。"""
    sess_dir = _mutbot_path("sessions")
    if not sess_dir.is_dir():
        return []
    results = []
    for f in sess_dir.glob("*.json"):
        data = load_json(f)
        if data and data.get("id") in session_ids:
            results.append(data)
    return results
