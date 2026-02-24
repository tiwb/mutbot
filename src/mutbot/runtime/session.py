"""Session manager — session lifecycle, Agent assembly, and persistence.

Session 采用 mutobj.Declaration 体系，通过子类定义不同的 Session 类型。
Runtime 状态（agent、bridge 等）采用分离模式，由 SessionManager 内部维护。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mutobj
from mutagent.agent import Agent
from mutagent.client import LLMClient
from mutagent.config import Config
from mutagent.messages import Message, ToolCall, ToolResult
from mutagent.tools import ToolSet

from mutbot.runtime import storage
from mutbot.web.agent_bridge import AgentBridge, WebUserIO
from mutbot.web.serializers import serialize_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session Declaration 体系
# ---------------------------------------------------------------------------

class Session(mutobj.Declaration):
    """所有 Session 的基类"""

    id: str
    workspace_id: str
    title: str
    type: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    config: dict = mutobj.field(default_factory=dict)
    deleted: bool = False

    def serialize(self) -> dict:
        """序列化为可持久化的 dict"""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "type": self.type,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "config": self.config,
            "deleted": self.deleted,
        }


class AgentSession(Session):
    """Agent 对话 Session"""

    type: str = "agent"
    model: str = ""
    system_prompt: str = ""


class TerminalSession(Session):
    """终端 Session"""

    type: str = "terminal"


class DocumentSession(Session):
    """文档编辑 Session"""

    type: str = "document"
    file_path: str = ""
    language: str = ""


# ---------------------------------------------------------------------------
# Session 类型注册表（基于 mutobj 子类发现 API）
# ---------------------------------------------------------------------------

_type_map_cache: dict[str, type[Session]] = {}
_type_map_generation: int = -1


def _get_type_default(cls: type) -> str:
    """从 Declaration 子类的 'type' 属性描述符中读取默认值"""
    for klass in cls.__mro__:
        desc = klass.__dict__.get("type")
        if desc is not None and hasattr(desc, "has_default") and desc.has_default:
            val = desc.default
            if isinstance(val, str) and val:
                return val
    return ""


def get_session_type_map() -> dict[str, type[Session]]:
    """返回 type_name → Session 子类映射，注册表变化时自动刷新"""
    global _type_map_cache, _type_map_generation
    gen = mutobj.get_registry_generation()
    if gen != _type_map_generation:
        _type_map_generation = gen
        _type_map_cache = {}
        for cls in mutobj.discover_subclasses(Session):
            type_name = _get_type_default(cls)
            if type_name:
                _type_map_cache[type_name] = cls
    return _type_map_cache


def get_session_class(type_name: str) -> type[Session]:
    """通过类型名查找 Session 子类"""
    type_map = get_session_type_map()
    cls = type_map.get(type_name)
    if cls is None:
        raise ValueError(f"Unknown session type: {type_name!r}")
    return cls


# ---------------------------------------------------------------------------
# Session Runtime 状态（分离模式）
# ---------------------------------------------------------------------------

@dataclass
class SessionRuntime:
    """Session 的 runtime 状态基类（不参与序列化）"""
    pass


@dataclass
class AgentSessionRuntime(SessionRuntime):
    """Agent Session 的 runtime 状态"""
    agent: Agent | None = None
    bridge: AgentBridge | None = None


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _deserialize_message(d: dict) -> Message:
    """Reconstruct a Message from a serialized dict."""
    tool_calls = [
        ToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments", {}))
        for tc in d.get("tool_calls", [])
    ]
    tool_results = [
        ToolResult(
            tool_call_id=tr["tool_call_id"],
            content=tr.get("content", ""),
            is_error=tr.get("is_error", False),
        )
        for tr in d.get("tool_results", [])
    ]
    return Message(
        role=d["role"],
        content=d.get("content", ""),
        tool_calls=tool_calls,
        tool_results=tool_results,
    )


def _session_from_dict(data: dict) -> Session:
    """从持久化 dict 重建对应子类的 Session 实例"""
    session_type = data.get("type", "agent")
    type_map = get_session_type_map()
    cls = type_map.get(session_type, Session)
    return cls(
        id=data["id"],
        workspace_id=data.get("workspace_id", ""),
        title=data.get("title", ""),
        type=session_type,
        status=data.get("status", "ended"),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        config=data.get("config") or {},
        deleted=data.get("deleted", False),
    )


def create_agent(
    agent_config: dict[str, Any] | None = None,
    log_dir: Path | None = None,
    session_ts: str = "",
    messages: list[Message] | None = None,
) -> Agent:
    """Assemble a mutagent Agent from configuration.

    Loads Config from the standard location, builds LLMClient + ToolSet,
    and returns a ready-to-run Agent.

    Args:
        messages: If provided, restore these messages into the new Agent
                  (for session resumption after restart).
    """
    import importlib
    import os
    import sys
    from pathlib import Path as _Path

    from mutagent.main import App
    from mutagent.toolkits.module_toolkit import ModuleToolkit
    from mutagent.toolkits.log_toolkit import LogToolkit
    from mutagent.runtime.module_manager import ModuleManager
    from mutagent.runtime.log_store import LogStore, LogStoreHandler
    from mutagent.runtime.api_recorder import ApiRecorder

    # Load config
    config = Config.load(".mutagent/config.json")

    # Set env vars
    for key, value in config.get("env", {}).items():
        os.environ[key] = value

    # Setup sys.path
    for mutagent_dir in [
        str(_Path.home() / ".mutagent"),
        str(_Path.cwd() / ".mutagent"),
    ]:
        if mutagent_dir not in sys.path:
            sys.path.insert(0, mutagent_dir)
    for p in config.get("path", []):
        if p not in sys.path:
            sys.path.insert(0, p)

    # Load extension modules
    for module_name in config.get("modules", []):
        importlib.import_module(module_name)

    # Get model config (convert SystemExit to RuntimeError for web context)
    model_name = (agent_config or {}).get("model")
    try:
        model = config.get_model(model_name)
    except SystemExit as e:
        raise RuntimeError(str(e)) from None

    # Build components
    search_dirs = [_Path.home() / ".mutagent", _Path.cwd() / ".mutagent"]
    module_manager = ModuleManager(search_dirs=search_dirs)
    module_tools = ModuleToolkit(module_manager=module_manager)

    log_store = LogStore()
    log_tools = LogToolkit(log_store=log_store)

    tool_set = ToolSet(auto_discover=True)
    tool_set.add(module_tools)
    tool_set.add(log_tools)

    # API call recorder (JSONL, shared log_dir with mutbot)
    api_recorder = None
    if log_dir and session_ts:
        api_recorder = ApiRecorder(log_dir, mode="incremental", session_ts=session_ts)
        logger.info("API recorder enabled (session_ts=%s)", session_ts)

    client = LLMClient(
        model=model.get("model_id", ""),
        api_key=model.get("auth_token", ""),
        base_url=model.get("base_url", ""),
        api_recorder=api_recorder,
    )

    system_prompt = (agent_config or {}).get("system_prompt", "")
    if not system_prompt:
        system_prompt = (
            "You are a Python AI Agent with the ability to inspect, modify, "
            "and run Python code at runtime. Use the available tools to help "
            "the user with their tasks."
        )

    agent = Agent(
        client=client,
        tool_set=tool_set,
        system_prompt=system_prompt,
        messages=messages if messages is not None else [],
    )
    tool_set.agent = agent
    return agent


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """Session 注册表，管理 Agent 生命周期和持久化。

    采用分离模式：Session Declaration 只描述配置/元数据，
    runtime 状态（agent、bridge 等）由 _runtimes 字典维护。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._runtimes: dict[str, SessionRuntime] = {}
        # Set by server.py lifespan for log/API recording
        self.session_ts: str = ""
        self.log_dir: Path | None = None
        # Set by server.py lifespan for terminal session management
        self.terminal_manager: Any = None

    # --- Runtime 访问 ---

    def get_runtime(self, session_id: str) -> SessionRuntime | None:
        return self._runtimes.get(session_id)

    def get_agent_runtime(self, session_id: str) -> AgentSessionRuntime | None:
        rt = self._runtimes.get(session_id)
        return rt if isinstance(rt, AgentSessionRuntime) else None

    # --- 持久化 ---

    def load_from_disk(self) -> None:
        """Load all sessions from .mutbot/sessions/*.json (metadata only)."""
        for data in storage.load_all_sessions():
            session = _session_from_dict(data)
            self._sessions[session.id] = session
        if self._sessions:
            logger.info("Loaded %d session(s) from disk", len(self._sessions))

    def _persist(self, session: Session) -> None:
        """Save session metadata + messages to disk."""
        data = session.serialize()
        # 如果有 agent runtime，一并保存 messages
        rt = self.get_agent_runtime(session.id)
        if rt and rt.agent and rt.agent.messages:
            data["messages"] = [serialize_message(m) for m in rt.agent.messages]
        storage.save_session_metadata(data)

    def _load_agent_messages(self, session_id: str) -> list[Message]:
        """Load saved messages from session JSON on disk."""
        data = storage.load_session_metadata(session_id)
        if not data or "messages" not in data:
            return []
        return [_deserialize_message(m) for m in data["messages"]]

    # --- 事件记录 ---

    def record_event(self, session_id: str, event_data: dict) -> None:
        """Append an event to the session's JSONL log.

        Assigns a unique ``event_id`` if not already present.
        Also persists session metadata on response_done/turn_done.
        """
        if "event_id" not in event_data:
            event_data["event_id"] = uuid.uuid4().hex[:16]
        storage.append_session_event(session_id, event_data)
        etype = event_data.get("type", "")
        if etype in ("response_done", "turn_done"):
            session = self._sessions.get(session_id)
            if session:
                self._persist(session)

    def get_session_events(self, session_id: str) -> list[dict]:
        return storage.load_session_events(session_id)

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
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist(session)
        return session

    def delete(self, session_id: str) -> bool:
        """Soft-delete a session."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.deleted = True
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist(session)
        return True

    def create(
        self,
        workspace_id: str,
        session_type: str = "agent",
        config: dict[str, Any] | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> Session:
        now = datetime.now(timezone.utc).isoformat()

        # 查找对应的 Session 子类
        cls = get_session_class(session_type)

        # 自动生成标题
        type_counts = sum(1 for s in self._sessions.values() if s.type == session_type)
        type_labels = {"agent": "Agent", "terminal": "Terminal", "document": "Document"}
        label = type_labels.get(session_type, session_type.capitalize())
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
        self._persist(session)
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_by_workspace(self, workspace_id: str) -> list[Session]:
        return [
            s for s in self._sessions.values()
            if s.workspace_id == workspace_id and not s.deleted
        ]

    # --- Agent 生命周期 ---

    def start(self, session_id: str, loop: asyncio.AbstractEventLoop, broadcast_fn=None) -> AgentBridge:
        """Assemble Agent + bridge and start the agent thread (agent sessions only).

        If the session has persisted messages, restore them into the new Agent.

        Args:
            broadcast_fn: async callable(session_id, data) for event broadcasting.
        """
        session = self._sessions[session_id]
        if not isinstance(session, AgentSession):
            raise ValueError(f"Cannot start agent bridge for {session.type!r} session")

        # 如果已有 runtime，返回现有 bridge
        rt = self.get_agent_runtime(session_id)
        if rt and rt.bridge is not None:
            return rt.bridge

        # Restore messages from disk if available
        saved_messages = self._load_agent_messages(session_id)
        if saved_messages:
            logger.info("Session %s: restoring %d messages", session_id, len(saved_messages))

        agent = create_agent(
            log_dir=self.log_dir,
            session_ts=self.session_ts,
            messages=saved_messages if saved_messages else None,
        )

        input_q: queue.Queue = queue.Queue()

        def _event_callback(data):
            loop.call_soon_threadsafe(bridge._event_queue.put_nowait, data)

        # Create event recorder bound to this session
        sm = self

        def _event_recorder(data):
            sm.record_event(session_id, data)

        web_userio = WebUserIO(input_queue=input_q, event_callback=_event_callback)
        bridge = AgentBridge(
            session_id, agent, web_userio, loop, broadcast_fn,
            event_recorder=_event_recorder,
        )

        # 存储 runtime 状态
        self._runtimes[session_id] = AgentSessionRuntime(agent=agent, bridge=bridge)

        bridge.start()
        logger.info("Session %s: agent started", session_id)
        return bridge

    async def stop(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return

        if isinstance(session, AgentSession):
            # Stop agent bridge
            rt = self.get_agent_runtime(session_id)
            if rt and rt.bridge is not None:
                await rt.bridge.stop()
            # Persist final state before clearing runtime
            self._persist(session)
            self._runtimes.pop(session_id, None)
        elif isinstance(session, TerminalSession):
            # Kill the associated PTY
            tm = self.terminal_manager
            if tm is not None and session.config:
                terminal_id = session.config.get("terminal_id")
                if terminal_id and tm.has(terminal_id):
                    await tm.async_notify_exit(terminal_id)
                    tm.kill(terminal_id)
        # Document sessions have no runtime resources to clean up

        session.status = "ended"
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist(session)
        logger.info("Session %s (%s): stopped", session_id, session.type)
