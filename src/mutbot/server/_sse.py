"""SSE（Server-Sent Events）响应格式化。"""

from __future__ import annotations


def format_sse(data: str, event: str | None = None, id: str | None = None) -> bytes:
    """格式化单条 SSE 消息。"""
    lines: list[str] = []
    if id is not None:
        lines.append(f"id: {id}")
    if event is not None:
        lines.append(f"event: {event}")
    for line in data.split("\n"):
        lines.append(f"data: {line}")
    lines.append("")
    lines.append("")
    return "\n".join(lines).encode("utf-8")
