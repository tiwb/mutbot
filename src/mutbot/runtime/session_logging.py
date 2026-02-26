"""Session 级日志隔离 — ContextVar + Filter。

通过 ``contextvars.ContextVar`` 标记当前 asyncio Task 所属的 session_id，
配合 ``SessionFilter`` 实现 per-session 的日志文件隔离。

用法：
    1. Agent Task 启动时调用 ``current_session_id.set(session_id)``
    2. 创建带 ``SessionFilter`` 的 FileHandler 并挂到 root logger
    3. Session 停止时移除 handler
"""

from __future__ import annotations

import contextvars
import logging

# 标记当前 asyncio task 所属的 session_id
current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_session_id", default=""
)


class SessionFilter(logging.Filter):
    """仅放行来自指定 session context 的日志记录。"""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self._session_id = session_id

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return current_session_id.get("") == self._session_id
