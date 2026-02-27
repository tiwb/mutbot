"""Session 实现细节 — SessionManager、Runtime 状态、Agent 组装。

Session Declaration 基类已迁移到 mutbot.session（公开 API），
本模块保留 runtime 实现：SessionManager 生命周期管理、持久化、Agent 组装。
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
from mutagent.agent import Agent
from mutagent.client import LLMClient
from mutagent.config import Config
from mutagent.messages import Message, ToolCall, ToolResult
from mutagent.tools import ToolSet

from mutbot.session import (
    Session,
    AgentSession,
    TerminalSession,
    DocumentSession,
)
from mutbot.runtime import storage
from mutbot.web.agent_bridge import AgentBridge
from mutbot.web.serializers import serialize_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session.get_session_class 实现
# ---------------------------------------------------------------------------

@mutobj.impl(Session.get_session_class)
def get_session_class(qualified_name: str) -> type[Session]:
    for cls in mutobj.discover_subclasses(Session):
        if f"{cls.__module__}.{cls.__qualname__}" == qualified_name:
            return cls
    raise ValueError(f"Unknown session type: {qualified_name!r}")


@mutobj.impl(Session.serialize)
def serialize_session(self: Session) -> dict:
    """序列化为可持久化的 dict（自动包含子类字段）"""
    d: dict[str, Any] = {
        "id": self.id,
        "workspace_id": self.workspace_id,
        "title": self.title,
        "type": self.type,
        "status": self.status,
        "created_at": self.created_at,
        "updated_at": self.updated_at,
        "config": self.config,
    }
    # AgentSession 特有字段
    if isinstance(self, AgentSession):
        if self.model:
            d["model"] = self.model
        if self.system_prompt:
            d["system_prompt"] = self.system_prompt
        if self.total_tokens:
            d["total_tokens"] = self.total_tokens
        if self.context_used:
            d["context_used"] = self.context_used
        if self.context_window:
            d["context_window"] = self.context_window
    # DocumentSession 特有字段
    if isinstance(self, DocumentSession):
        if self.file_path:
            d["file_path"] = self.file_path
        if self.language:
            d["language"] = self.language
    return d

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
    log_handler: logging.Handler | None = None  # session 级日志 FileHandler


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _build_session_prefix(session: Session, session_id: str) -> str:
    """从 session.created_at (ISO UTC) 构建文件名前缀。

    返回 ``session-YYYYMMDD_HHMMSS-{session_id}``，时间转为本地时区。
    如果 created_at 解析失败，回退到当前时间。
    """
    ts_str = ""
    if session.created_at:
        try:
            dt_utc = datetime.fromisoformat(session.created_at)
            dt_local = dt_utc.astimezone()
            ts_str = dt_local.strftime("%Y%m%d_%H%M%S")
        except (ValueError, OSError):
            pass
    if not ts_str:
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"session-{ts_str}-{session_id}"


def _create_session_log_handler(
    log_dir: Path | None,
    session_prefix: str,
    session_id: str,
) -> logging.Handler | None:
    """创建 session 级 FileHandler 并挂到 root logger。

    返回 handler 引用（stop 时需要移除），log_dir 为 None 时返回 None。
    """
    if not log_dir:
        return None

    from mutbot.runtime.session_logging import SessionFilter
    from mutagent.runtime.log_store import SingleLineFormatter

    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(
        log_dir / f"{session_prefix}.log", encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(SingleLineFormatter(
        "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
    ))
    handler.addFilter(SessionFilter(session_id))

    logging.getLogger().addHandler(handler)
    return handler


def _remove_session_log_handler(handler: logging.Handler | None) -> None:
    """从 root logger 移除 session 级 FileHandler 并关闭。"""
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    handler.close()


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
    """从持久化 dict 重建对应子类的 Session 实例。"""
    raw_type = data.get("type", "")

    # 尝试查找 Session 子类
    try:
        cls = Session.get_session_class(raw_type)
    except ValueError:
        cls = Session

    kwargs: dict[str, Any] = {
        "id": data["id"],
        "workspace_id": data.get("workspace_id", ""),
        "title": data.get("title", ""),
        "type": raw_type,
        "status": data.get("status", "ended"),
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "config": data.get("config") or {},
    }
    # AgentSession 特有字段
    if issubclass(cls, AgentSession):
        if "model" in data:
            kwargs["model"] = data["model"]
        if "system_prompt" in data:
            kwargs["system_prompt"] = data["system_prompt"]
        if "total_tokens" in data:
            kwargs["total_tokens"] = data["total_tokens"]
        if "context_used" in data:
            kwargs["context_used"] = data["context_used"]
        if "context_window" in data:
            kwargs["context_window"] = data["context_window"]
    # DocumentSession 特有字段
    if issubclass(cls, DocumentSession):
        if "file_path" in data:
            kwargs["file_path"] = data["file_path"]
        if "language" in data:
            kwargs["language"] = data["language"]

    return cls(**kwargs)


def setup_environment(config: Config) -> None:
    """执行全局环境初始化（env、sys.path、扩展模块加载）。

    可被多次调用，操作幂等。
    """
    import importlib
    import os
    import sys
    from pathlib import Path as _Path

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


def _load_config() -> dict | None:
    """加载 mutbot 配置文件，返回 providers 相关配置 dict。供 proxy 等模块使用。"""
    try:
        from mutbot.runtime.config import load_mutbot_config
        config = load_mutbot_config()
        return {
            "providers": config.get("providers", {}),
            "default_model": config.get("default_model", ""),
        }
    except Exception:
        return None


def create_llm_client(
    config: Config,
    model_name: str = "",
    log_dir: Path | None = None,
    session_ts: str = "",
) -> LLMClient:
    """根据 Config 创建 LLMClient 实例（含可选的 API 录制器）。"""
    import mutobj
    from mutagent.runtime.api_recorder import ApiRecorder
    from mutagent.provider import LLMProvider

    # 确保内置 provider 已注册
    import mutagent.builtins.anthropic_provider  # noqa: F401
    import mutagent.builtins.openai_provider  # noqa: F401

    # Get model config (convert SystemExit to RuntimeError for web context)
    try:
        model = config.get_model(model_name or None)
    except SystemExit as e:
        raise RuntimeError(str(e)) from None

    # API call recorder (JSONL, shared log_dir with mutbot)
    api_recorder = None
    if log_dir and session_ts:
        api_recorder = ApiRecorder(log_dir, mode="incremental", session_ts=session_ts)
        logger.info("API recorder enabled (session_ts=%s)", session_ts)

    # 通过 provider 配置创建 provider 实例
    provider_path = model.get("provider", "AnthropicProvider")
    provider_cls = mutobj.resolve_class(provider_path, base_cls=LLMProvider)
    provider = provider_cls.from_config(model)

    # context_window: 配置优先，内置查找表兜底
    from mutagent.client import get_model_context_window
    model_id = model.get("model_id", "")
    context_window = model.get("context_window")
    if context_window is None:
        context_window = get_model_context_window(model_id)

    return LLMClient(
        provider=provider,
        model=model_id,
        context_window=context_window,
        api_recorder=api_recorder,
    )


def build_default_agent(
    session: AgentSession,
    config: Config,
    log_dir: Path | None = None,
    session_ts: str = "",
    messages: list[Message] | None = None,
) -> Agent:
    """AgentSession 基类的默认 create_agent 实现。

    组装 ModuleToolkit + LogToolkit + auto_discover 的标准 Agent。
    """
    from mutagent.toolkits.module_toolkit import ModuleToolkit
    from mutagent.toolkits.log_toolkit import LogToolkit
    from mutagent.runtime.module_manager import ModuleManager
    from mutagent.runtime.log_store import LogStore

    setup_environment(config)

    client = create_llm_client(config, session.model, log_dir, session_ts)

    # Build tool set
    search_dirs = [Path.home() / ".mutagent", Path.cwd() / ".mutagent"]
    module_manager = ModuleManager(search_dirs=search_dirs)
    module_tools = ModuleToolkit(module_manager=module_manager)

    log_store = LogStore()
    log_tools = LogToolkit(log_store=log_store)

    tool_set = ToolSet(auto_discover=True)
    tool_set.add(module_tools)
    tool_set.add(log_tools)

    system_prompt = session.system_prompt
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
        self._config: Config | None = None
        # Set by server.py lifespan for per-session API recording
        self.log_dir: Path | None = None
        # Set by server.py lifespan for terminal session management
        self.terminal_manager: Any = None
        # 跨线程广播支持（Agent 线程创建 Session 时使用）
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._broadcast_fn: Any = None  # async callable(event_data: dict)

    def _get_config(self) -> Config:
        """懒加载 Config（首次调用时读取配置文件）。"""
        if self._config is None:
            from mutbot.runtime.config import load_mutbot_config
            self._config = load_mutbot_config()
        return self._config

    # --- Runtime 访问 ---

    def get_runtime(self, session_id: str) -> SessionRuntime | None:
        return self._runtimes.get(session_id)

    def get_agent_runtime(self, session_id: str) -> AgentSessionRuntime | None:
        rt = self._runtimes.get(session_id)
        return rt if isinstance(rt, AgentSessionRuntime) else None

    # --- 持久化 ---

    def load_from_disk(self, session_ids: set[str] | None = None) -> None:
        """Load sessions from disk.

        Args:
            session_ids: 要加载的 session ID 集合。为 None 时加载全部（兼容旧调用）。
        """
        if session_ids is not None:
            raw_list = storage.load_sessions(session_ids)
        else:
            raw_list = storage.load_all_sessions()
        for data in raw_list:
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
        else:
            # 没有 runtime 时保留磁盘上已有的 messages（避免覆写丢失）
            existing = storage.load_session_metadata(session.id)
            if existing and "messages" in existing:
                data["messages"] = existing["messages"]
        storage.save_session_metadata(data)

    def _load_agent_messages(self, session_id: str) -> list[Message]:
        """Load saved messages from session JSON on disk."""
        data = storage.load_session_metadata(session_id)
        if not data or "messages" not in data:
            return []
        return [_deserialize_message(m) for m in data["messages"]]

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
        """从内存中移除 session（磁盘文件保留，便于恢复）。"""
        if session_id not in self._sessions:
            return False
        self._sessions.pop(session_id)
        self._runtimes.pop(session_id, None)
        return True

    def create(
        self,
        workspace_id: str,
        session_type: str,
        config: dict[str, Any] | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> Session:
        now = datetime.now(timezone.utc).isoformat()

        # 查找对应的 Session 子类
        cls = Session.get_session_class(session_type)

        # 自动生成标题：从类名推导标签
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
        """设置广播回调（由 workspace WebSocket handler 调用）。

        Args:
            loop: 事件循环（用于从 Agent 线程安全调度异步调用）。
            broadcast_fn: async callable(workspace_id: str, event_data: dict)，
                          广播到指定 workspace 的所有客户端。
        """
        self._event_loop = loop
        self._broadcast_fn = broadcast_fn

    def _maybe_broadcast_created(self, session: Session) -> None:
        """如果配置了广播回调，广播 session_created 事件。"""
        if self._broadcast_fn is None or self._event_loop is None:
            return
        # 构建事件数据（与 routes.py _session_dict 格式一致）
        data = session.serialize()
        # 添加 kind 字段
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
        # 从任意线程安全调度到事件循环
        try:
            loop.call_soon_threadsafe(
                asyncio.ensure_future, broadcast(workspace_id, event)
            )
        except RuntimeError:
            # 事件循环已关闭
            pass

    # --- Agent 生命周期 ---

    def start(self, session_id: str, loop: asyncio.AbstractEventLoop, broadcast_fn=None) -> AgentBridge:
        """Assemble Agent + bridge and start the agent thread (agent sessions only).

        If the session has persisted messages, restore them into the new Agent.

        Args:
            broadcast_fn: async callable(session_id, data) for event broadcasting.
        """
        session = self._sessions[session_id]
        if not isinstance(session, AgentSession):
            raise ValueError(f"Cannot start agent bridge for {type(session).__name__} session")

        # 如果已有 runtime，返回现有 bridge
        rt = self.get_agent_runtime(session_id)
        if rt and rt.bridge is not None:
            return rt.bridge

        # Restore messages from disk if available
        saved_messages = self._load_agent_messages(session_id)
        if saved_messages:
            logger.info("Session %s: restoring %d messages", session_id, len(saved_messages))
        else:
            logger.info("Session %s: no saved messages to restore", session_id)

        config = self._get_config()
        # 构建 session 文件名前缀：session-YYYYMMDD_HHMMSS-{session_id}
        session_prefix = _build_session_prefix(session, session_id)
        agent = session.create_agent(
            config=config,
            log_dir=self.log_dir,
            session_ts=session_prefix,
            messages=saved_messages if saved_messages else None,
            session_manager=self,
        )

        # Create persist callback bound to this session
        sm = self

        def _persist_fn():
            sm._persist(session)

        bridge = AgentBridge(
            session_id, agent, loop, broadcast_fn,
            session=session,
            persist_fn=_persist_fn,
        )

        # --- Session 级日志 FileHandler ---
        session_log_handler = _create_session_log_handler(
            self.log_dir, session_prefix, session_id,
        )

        # 存储 runtime 状态
        self._runtimes[session_id] = AgentSessionRuntime(
            agent=agent, bridge=bridge, log_handler=session_log_handler,
        )

        bridge.start()
        logger.info("Session %s: agent started", session_id)

        # 重新激活 ended session（用户主动打开时恢复为 active）
        if session.status == "ended":
            session.status = "active"
            session.updated_at = datetime.now(timezone.utc).isoformat()
            self._persist(session)
            logger.info("Session %s: reactivated (ended → active)", session_id)

        # 如果 config 中有 initial_message，自动作为隐藏消息发送（不显示在聊天界面）
        initial_message = session.config.pop("initial_message", None)
        if initial_message:
            bridge.send_message(initial_message, data={"hidden": True})
            self._persist(session)
            logger.info("Session %s: sent initial_message (hidden, %d chars)", session_id, len(initial_message))

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
            # 移除 session 级日志 handler
            if rt:
                _remove_session_log_handler(rt.log_handler)
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
        # Persist final state (runtime still available so messages are included)
        self._persist(session)
        # Now safe to clear runtime
        self._runtimes.pop(session_id, None)
        logger.info("Session %s (%s): stopped", session_id, type(session).__name__)
