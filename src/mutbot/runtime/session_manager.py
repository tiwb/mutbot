"""Session \u5b9e\u73b0\u7ec6\u8282 \u2014 SessionManager\u3001Runtime \u72b6\u6001\u3002

Session Declaration \u57fa\u7c7b\u5728 mutbot.session\uff08\u516c\u5f00 API\uff09\uff0c
\u672c\u6a21\u5757\u4fdd\u7559 runtime \u5b9e\u73b0\uff1aSessionManager \u751f\u547d\u5468\u671f\u7ba1\u7406\u3001\u6301\u4e45\u5316\u3002

Agent \u76f8\u5173\u7ec4\u88c5\u903b\u8f91\uff08build_default_agent / AgentSessionRuntime / start\uff09
\u968f\u672c\u6b21\u91cd\u6784\u5265\u79bb\uff0c\u4ee3\u7801\u4ee5\u6e90\u4ed3 git \u5386\u53f2\u4e3a\u51c6\uff0c\u672a\u6765\u91cd\u63a5\u5165 mutagent \u65f6\u4ece\u5386\u53f2\u6062\u590d\u3002
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mutobj
from mutbot.runtime.config import Config

from mutbot.session import Session
from mutbot.runtime import storage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session.get_session_class 实现
# ---------------------------------------------------------------------------

@mutobj.impl(Session.get_session_class)
def session_get_session_class(qualified_name: str) -> type[Session]:
    for cls in mutobj.discover_subclasses(Session):
        if f"{cls.__module__}.{cls.__qualname__}" == qualified_name:
            return cls
    raise ValueError(f"Unknown session type: {qualified_name!r}")


@mutobj.impl(Session.serialize)
def session_serialize(self: Session) -> dict:
    """序列化为可持久化的 dict（基于 __annotations__ 自动收集所有声明字段）"""
    d: dict[str, Any] = {}
    for cls in type(self).__mro__:
        if cls is object or cls.__name__ == "Declaration":
            continue
        for attr_name in getattr(cls, "__annotations__", {}):
            if attr_name in d:
                continue  # 子类已处理
            value = getattr(self, attr_name, None)
            if attr_name in ("id", "workspace_id", "title", "type",
                             "status", "created_at", "updated_at", "config"):
                d[attr_name] = value if value is not None else ""
            elif value:
                d[attr_name] = value
    return d


# ---------------------------------------------------------------------------
# Session Runtime 状态（分离模式）
# ---------------------------------------------------------------------------

@dataclass
class SessionRuntime:
    """Session 的 runtime 状态基类（不参与序列化）"""
    pass


@mutobj.impl(Session.deserialize)
def session_deserialize(cls: type[Session], data: dict) -> Session:
    """从持久化 dict 重建对应子类的 Session 实例（基于 __annotations__ 自动提取字段）。"""
    raw_type = data.get("type", "")

    # 查找 Session 子类；找不到（如 AgentSession 已被剥离）回退到 Session 基类
    try:
        target_cls = Session.get_session_class(raw_type)
    except ValueError:
        target_cls = Session

    kwargs: dict[str, Any] = {}
    for klass in target_cls.__mro__:
        if klass is object or klass.__name__ == "Declaration":
            continue
        for attr_name in getattr(klass, "__annotations__", {}):
            if attr_name in kwargs:
                continue
            if attr_name in data:
                kwargs[attr_name] = data[attr_name]

    kwargs.setdefault("id", data.get("id", ""))
    kwargs.setdefault("workspace_id", data.get("workspace_id", ""))
    kwargs.setdefault("title", data.get("title", ""))
    kwargs.setdefault("type", raw_type)
    if "config" in kwargs and kwargs["config"] is None:
        kwargs["config"] = {}

    return target_cls(**kwargs)


def setup_environment(config: Config) -> None:
    """执行全局环境初始化（env、sys.path、扩展模块加载）。

    可被多次调用，操作幂等。
    """
    import importlib
    import os
    import sys
    from pathlib import Path as _Path

    for key, value in config.get("env", default={}).items():
        os.environ[key] = value

    for mutagent_dir in [
        str(_Path.home() / ".mutagent"),
        str(_Path.cwd() / ".mutagent"),
    ]:
        if mutagent_dir not in sys.path:
            sys.path.insert(0, mutagent_dir)
    for p in config.get("path", default=[]):
        if p not in sys.path:
            sys.path.insert(0, p)

    for module_name in config.get("modules", default=[]):
        importlib.import_module(module_name)


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """Session 注册表，管理生命周期和持久化。

    Agent 已剥离：当前只承载 TerminalSession 等非 agent session 的生命周期。
    """

    def __init__(self, config: Config | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._runtimes: dict[str, SessionRuntime] = {}
        self.config: Config = config or Config()
        self.log_dir: Path | None = None
        self.terminal_manager: Any = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._broadcast_fn: Any = None
        self._dirty: set[str] = set()

    # --- Runtime 访问 ---

    def get_runtime(self, session_id: str) -> SessionRuntime | None:
        return self._runtimes.get(session_id)

    def get_agent_runtime(self, session_id: str) -> SessionRuntime | None:
        """Agent 已剥离，统一返回 None（保留 API 形状以减少调用方改动）。"""
        return None

    def get_bridge(self, session_id: str) -> Any:
        """Agent 已剥离，无 bridge。"""
        return None

    # --- 持久化 ---

    def load_from_disk(self, session_ids: set[str] | None = None) -> None:
        if session_ids is not None:
            raw_list = storage.load_sessions(session_ids)
        else:
            raw_list = storage.load_all_sessions()
        for data in raw_list:
            # 跳过类型已不存在的 session（如 AgentSession 已剥离）
            raw_type = data.get("type", "")
            try:
                Session.get_session_class(raw_type)
            except ValueError:
                logger.debug("Skipping unknown session type: %s", raw_type)
                continue
            session = Session.deserialize(data)
            self._sessions[session.id] = session
        if self._sessions:
            logger.info("Loaded %d session(s) from disk", len(self._sessions))

    def _persist(self, session: Session) -> None:
        """Save session metadata to disk.

        Agent 已剥离：messages 字段保留磁盘上已有的内容（避免覆写丢失）。
        """
        data = session.serialize()
        existing = storage.load_session_metadata(session.id)
        if existing and "messages" in existing:
            data["messages"] = existing["messages"]
        storage.save_session_metadata(data)

    def mark_dirty(self, session_id: str) -> None:
        self._dirty.add(session_id)

    async def persist_dirty_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            dirty, self._dirty = self._dirty, set()
            for sid in dirty:
                session = self._sessions.get(sid)
                if session:
                    self._persist(session)

    # --- CRUD ---

    def update(self, session_id: str, **fields: Any) -> Session | None:
        """Update session fields (title, config, status, …) and persist."""
        session = self._sessions.get(session_id)
        if not session:
            return None
        if "title" in fields:
            session.title = fields["title"]
        if "config" in fields:
            session.config.update(fields["config"])
        if "status" in fields:
            session.status = fields["status"]
        # model 字段属于 AgentSession，agent 剥离后忽略
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist(session)
        return session

    def delete(self, session_id: str) -> bool:
        if session_id not in self._sessions:
            return False
        self._sessions.pop(session_id)
        self._runtimes.pop(session_id, None)
        return True

    async def create(
        self,
        workspace_id: str,
        session_type: str,
        config: dict[str, Any] | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> Session:
        now = datetime.now(timezone.utc).isoformat()

        cls = Session.get_session_class(session_type)

        type_counts = sum(1 for s in self._sessions.values() if type(s) is cls)
        label = cls.__name__
        if label.endswith("Session"):
            label = label[:-7]
        title = f"{label} {type_counts + 1}"

        session = cls(
            id=uuid.uuid4().hex[:12],
            workspace_id=workspace_id,
            title=title,
            created_at=now,
            updated_at=now,
            config=config or {},
        )
        self._sessions[session.id] = session
        await session.on_create(self)
        self._persist(session)
        self._maybe_broadcast_created(session)
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_by_workspace(self, workspace_id: str) -> list[Session]:
        return [
            s for s in self._sessions.values()
            if s.workspace_id == workspace_id
        ]

    # --- 跨线程广播 ---

    def set_broadcast(self, loop: asyncio.AbstractEventLoop, broadcast_fn: Any) -> None:
        self._event_loop = loop
        self._broadcast_fn = broadcast_fn

    def _maybe_broadcast_created(self, session: Session) -> None:
        if self._broadcast_fn is None or self._event_loop is None:
            return
        data = session.serialize()
        qname = data.get("type", "")
        parts = qname.rsplit(".", 1)
        name = parts[-1] if parts else qname
        if name.endswith("Session"):
            name = name[:-7]
        data["kind"] = name.lower()

        event = {"type": "event", "event": "session_created", "data": data}
        loop = self._event_loop
        broadcast = self._broadcast_fn
        workspace_id = session.workspace_id
        try:
            loop.call_soon_threadsafe(
                asyncio.ensure_future, broadcast(workspace_id, event)
            )
        except RuntimeError:
            pass

    def _maybe_broadcast_updated(self, session: Session) -> None:
        if self._broadcast_fn is None or self._event_loop is None:
            return
        data = session.serialize()
        qname = data.get("type", "")
        parts = qname.rsplit(".", 1)
        name = parts[-1] if parts else qname
        if name.endswith("Session"):
            name = name[:-7]
        data["kind"] = name.lower()
        data["icon"] = session.config.get("icon") or getattr(type(session), "display_icon", "") or ""

        event = {"type": "event", "event": "session_updated", "data": data}
        loop = self._event_loop
        broadcast = self._broadcast_fn
        workspace_id = session.workspace_id
        try:
            loop.call_soon_threadsafe(
                asyncio.ensure_future, broadcast(workspace_id, event)
            )
        except RuntimeError:
            pass

    def set_session_status(self, session_id: str, status: str) -> None:
        session = self._sessions.get(session_id)
        if session is None or session.status == status:
            return
        session.status = status
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist(session)
        self._maybe_broadcast_updated(session)

    # --- 生命周期 ---

    async def stop(self, session_id: str) -> None:
        """停止 session（agent 剥离后只走子类 on_stop 钩子）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return

        session.on_stop(self)
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist(session)
        self._runtimes.pop(session_id, None)
        logger.info("Session %s (%s): stopped", session_id, type(session).__name__)
