"""File-based persistence — JSON / JSONL with atomic writes."""

from __future__ import annotations

import json
import logging
import os
import tempfile
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


def append_jsonl(path: Path, record: dict) -> None:
    """Append a single JSON line to a JSONL file."""
    _ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    """Load all lines from a JSONL file, skipping corrupt lines."""
    if not path.is_file():
        return []
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping corrupt line %d in %s", lineno, path)
    return records


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def _mutbot_path(*parts: str) -> Path:
    return Path(MUTBOT_DIR).joinpath(*parts)


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
    path = _mutbot_path("sessions", f"{session_data['id']}.json")
    save_json(path, session_data)


def load_session_metadata(session_id: str) -> dict | None:
    return load_json(_mutbot_path("sessions", f"{session_id}.json"))


def load_all_sessions() -> list[dict]:
    sess_dir = _mutbot_path("sessions")
    if not sess_dir.is_dir():
        return []
    results = []
    for f in sess_dir.glob("*.json"):
        if f.name.endswith(".events.jsonl"):
            continue
        data = load_json(f)
        if data and "id" in data:
            results.append(data)
    return results


def append_session_event(session_id: str, event_data: dict) -> None:
    path = _mutbot_path("sessions", f"{session_id}.events.jsonl")
    append_jsonl(path, event_data)


def load_session_events(session_id: str) -> list[dict]:
    path = _mutbot_path("sessions", f"{session_id}.events.jsonl")
    return load_jsonl(path)
