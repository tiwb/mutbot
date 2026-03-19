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

# mutbot 启动时的工作目录（server.py 启动时设置，daemon 模式 fallback 到 home）
STARTUP_CWD = str(Path.home())


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


def _find_workspace_file(workspace_id: str) -> Path | None:
    """按 workspace_id 查找文件，兼容新旧格式。

    新格式 ``{date}-{name}-{workspace_id}.json``，旧格式 ``{workspace_id}.json``，
    glob ``*{workspace_id}.json`` 均可匹配。
    """
    ws_dir = _mutbot_path("workspaces")
    if not ws_dir.is_dir():
        return None
    matches = list(ws_dir.glob(f"*{workspace_id}.json"))
    return matches[0] if matches else None


def _workspace_file_prefix(ws_data: dict) -> str:
    """从 workspace 数据构建文件名前缀 ``YYYYMMDD-{name}-``。"""
    created_at = ws_data.get("created_at", "")
    name = ws_data.get("name", "workspace")
    if created_at:
        dt_utc = datetime.fromisoformat(created_at)
        dt_local = dt_utc.astimezone()
        date_str = dt_local.strftime("%Y%m%d")
    else:
        date_str = datetime.now().strftime("%Y%m%d")
    return f"{date_str}-{name}-"


def save_workspace(ws_data: dict) -> None:
    ws_id = ws_data["id"]
    prefix = _workspace_file_prefix(ws_data)
    new_path = _mutbot_path("workspaces", f"{prefix}{ws_id}.json")
    save_json(new_path, ws_data)
    # 清理旧格式文件（避免同一 workspace 两个文件共存）
    old_path = _mutbot_path("workspaces", f"{ws_id}.json")
    if old_path.is_file() and old_path != new_path:
        try:
            old_path.unlink()
        except OSError:
            pass


def load_workspace(workspace_id: str) -> dict | None:
    """加载单个 workspace JSON 文件（兼容新旧格式）。"""
    path = _find_workspace_file(workspace_id)
    if path is None:
        return None
    return load_json(path)


def load_all_workspaces() -> list[dict]:
    ws_dir = _mutbot_path("workspaces")
    if not ws_dir.is_dir():
        return []
    results = []
    for f in ws_dir.glob("*.json"):
        if f.name == "registry.json":
            continue
        data = load_json(f)
        if data and "id" in data:
            results.append(data)
    return results


def load_workspace_registry() -> list[str]:
    """加载 workspace 注册表，返回 ID 列表。文件不存在返回空列表。"""
    path = _mutbot_path("workspaces", "registry.json")
    data = load_json(path)
    if data and isinstance(data.get("workspaces"), list):
        return data["workspaces"]
    return []


def save_workspace_registry(ids: list[str]) -> None:
    """原子写入 workspace 注册表。"""
    path = _mutbot_path("workspaces", "registry.json")
    save_json(path, {"workspaces": ids})


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
