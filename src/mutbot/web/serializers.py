"""Serialize mutbot dataclasses to JSON-safe dicts for WebSocket transport.

agent\u76f8\u5173\u7684 Message/Block/StreamEvent \u5e8f\u5217\u5316\u5df2\u968f agent \u529f\u80fd\u5269\u79bb\u4e00\u540c\u79fb\u9664\u3002
\u5982\u672a\u6765 agent \u80fd\u529b\u91cd\u65b0\u63a5\u5165\uff0c\u4ece\u4ed3\u5e93\u5386\u53f2\u6062\u590d\u8be5\u90e8\u5206\u3002
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Data dict 序列化（workspace / session / terminal）
# ---------------------------------------------------------------------------

def workspace_dict(ws) -> dict[str, Any]:
    return {
        "id": ws.id,
        "name": ws.name,
        "sessions": ws.sessions,
        "layout": ws.layout,
        "created_at": ws.created_at,
        "updated_at": ws.updated_at,
        "last_accessed_at": ws.last_accessed_at,
    }


def session_dict(s) -> dict[str, Any]:
    kind = session_kind(s.type)
    icon = s.config.get("icon") or getattr(type(s), "display_icon", "") or ""
    d: dict[str, Any] = {
        "id": s.id,
        "workspace_id": s.workspace_id,
        "title": s.title,
        "type": s.type,
        "kind": kind,
        "icon": icon,
        "status": s.status,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "config": s.config,
    }
    # AgentSession 已剥离；如未来重新接入，model 字段在此追加
    return d


def session_kind(session_type: str) -> str:
    """从全限定类型名推导短类型名。"""
    parts = session_type.rsplit(".", 1)
    name = parts[-1] if parts else session_type
    if name.endswith("Session"):
        name = name[:-7]
    return name.lower()


def session_type_display(qualified: str, cls: type) -> tuple[str, str]:
    """获取 Session 类型的 (显示名, 图标)。"""
    name = getattr(cls, "display_name", "") or ""
    icon = getattr(cls, "display_icon", "") or ""
    if not name:
        raw = cls.__name__
        if raw.endswith("Session"):
            raw = raw[:-7]
        name = raw
    if not icon:
        icon = session_kind(qualified)
    return (name, icon)


def terminal_dict(t) -> dict[str, Any]:
    return {
        "id": t.id,
        "workspace_id": t.workspace_id,
        "rows": t.rows,
        "cols": t.cols,
        "alive": t.alive,
    }


LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescriptreact",
    ".jsx": "javascriptreact", ".json": "json", ".html": "html", ".css": "css",
    ".md": "markdown", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".sh": "shell", ".bash": "shell", ".sql": "sql", ".xml": "xml",
    ".rs": "rust", ".go": "go", ".java": "java", ".c": "c", ".cpp": "cpp",
    ".h": "c", ".hpp": "cpp", ".rb": "ruby", ".php": "php",
}
