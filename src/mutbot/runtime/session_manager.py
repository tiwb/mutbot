"""Session 实现细节 — SessionManager、Runtime 状态、Agent 组装。

Session Declaration 基类已迁移到 mutbot.session（公开 API），
本模块保留 runtime 实现：SessionManager 生命周期管理、持久化、Agent 组装。
"""

from __future__ import annotations

import asyncio
import base64
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
from mutagent.context import AgentContext
from mutagent.messages import Message, TextBlock
from mutagent.tools import ToolSet

from mutbot.session import (
    Session,
    AgentSession,
    TerminalSession,
    DocumentSession,
)
from mutbot.runtime import storage
from mutbot.runtime.agent_bridge import AgentBridge
from mutbot.web.serializers import serialize_message, deserialize_message

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
    """序列化为可持久化的 dict（基于 __annotations__ 自动收集所有声明字段）"""
    d: dict[str, Any] = {}
    # 遍历 MRO 收集所有声明字段（跳过 object 和 Declaration 基类）
    for cls in type(self).__mro__:
        if cls is object or cls.__name__ == "Declaration":
            continue
        for attr_name in getattr(cls, "__annotations__", {}):
            if attr_name in d:
                continue  # 子类已处理
            value = getattr(self, attr_name, None)
            # 跳过空值/默认值（保持与原有行为一致）
            if attr_name in ("id", "workspace_id", "title", "type",
                             "status", "created_at", "updated_at", "config"):
                # 基类核心字段始终写入
                d[attr_name] = value if value is not None else ""
            elif value:
                # 子类扩展字段：非空/非零时写入
                d[attr_name] = value
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





@mutobj.impl(Session.deserialize)
def _deserialize_session(cls: type[Session], data: dict) -> Session:
    """从持久化 dict 重建对应子类的 Session 实例（基于 __annotations__ 自动提取字段）。"""
    raw_type = data.get("type", "")

    # 查找 Session 子类
    try:
        target_cls = Session.get_session_class(raw_type)
    except ValueError:
        target_cls = Session

    # 收集目标类的所有声明字段
    kwargs: dict[str, Any] = {}
    for klass in target_cls.__mro__:
        if klass is object or klass.__name__ == "Declaration":
            continue
        for attr_name in getattr(klass, "__annotations__", {}):
            if attr_name in kwargs:
                continue  # 子类已处理
            if attr_name in data:
                kwargs[attr_name] = data[attr_name]

    # 确保必填字段存在
    kwargs.setdefault("id", data.get("id", ""))
    kwargs.setdefault("workspace_id", data.get("workspace_id", ""))
    kwargs.setdefault("title", data.get("title", ""))
    kwargs.setdefault("type", raw_type)
    # config 特殊处理：None → {}
    if "config" in kwargs and kwargs["config"] is None:
        kwargs["config"] = {}

    return target_cls(**kwargs)


# ---------------------------------------------------------------------------
# 生命周期方法实现
# ---------------------------------------------------------------------------

# -- on_create --

# TerminalSession.on_create → runtime/terminal.py


@mutobj.impl(DocumentSession.on_create)
async def _document_on_create(self: DocumentSession, sm: SessionManager) -> None:
    """DocumentSession：设默认 file_path。"""
    import time
    if not self.file_path:
        self.file_path = self.config.get("file_path", "")
    if not self.file_path:
        self.file_path = f"untitled-{int(time.time() * 1000)}.md"


# -- on_stop --

@mutobj.impl(AgentSession.on_stop)
def _agent_on_stop(self: AgentSession, sm: SessionManager) -> None:
    """AgentSession：状态归位为空。"""
    self.status = ""

# TerminalSession.on_stop → runtime/terminal.py


# -- on_restart_cleanup --

@mutobj.impl(AgentSession.on_restart_cleanup)
def _agent_on_restart_cleanup(self: AgentSession) -> None:
    """AgentSession：running → 空。"""
    if self.status == "running":
        self.status = ""

# TerminalSession.on_restart_cleanup → runtime/terminal.py


def setup_environment(config: Config) -> None:
    """执行全局环境初始化（env、sys.path、扩展模块加载）。

    可被多次调用，操作幂等。
    """
    import importlib
    import os
    import sys
    from pathlib import Path as _Path

    # Set env vars
    for key, value in config.get("env", default={}).items():
        os.environ[key] = value

    # Setup sys.path
    for mutagent_dir in [
        str(_Path.home() / ".mutagent"),
        str(_Path.cwd() / ".mutagent"),
    ]:
        if mutagent_dir not in sys.path:
            sys.path.insert(0, mutagent_dir)
    for p in config.get("path", default=[]):
        if p not in sys.path:
            sys.path.insert(0, p)

    # Load extension modules
    for module_name in config.get("modules", default=[]):
        importlib.import_module(module_name)




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

    # Get model config
    model = LLMProvider.resolve_model(config, model_name or None)
    if model is None:
        raise RuntimeError(
            f"No model configured (requested: {model_name!r}). "
            "Run the setup wizard or check ~/.mutbot/config.json"
        )

    # API call recorder (JSONL, shared log_dir with mutbot)
    api_recorder = None
    if log_dir and session_ts:
        api_recorder = ApiRecorder(log_dir, mode="incremental", session_ts=session_ts)
        logger.info("API recorder enabled (session_ts=%s)", session_ts)

    # 通过 provider 配置创建 provider 实例
    provider_path = model.get("provider", "AnthropicProvider")
    provider_cls = mutobj.resolve_class(provider_path, base_cls=LLMProvider)
    provider = provider_cls.from_spec(model)

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

    手动组装基础工具集：WebToolkit + ConfigToolkit + UIToolkit。
    无 LLM 配置时使用 NullProvider 触发配置向导。
    """
    from mutbot.builtins.pysandbox_toolkit import PySandboxToolkit
    from mutbot.builtins.config_toolkit import ConfigToolkit, NullProvider
    from mutbot.ui.toolkit import UIToolkit

    setup_environment(config)

    # 有 LLM 配置 → 使用真实 provider；无配置 → NullProvider 占位
    if config.get("providers"):
        client = create_llm_client(config, session.model, log_dir, session_ts)
    else:
        client = LLMClient(
            provider=NullProvider(),
            model="setup-wizard",
        )

    # 手动组装基础工具集
    tool_set = ToolSet()

    # PySandbox — 从 server 获取 SandboxApp 单例
    from mutbot.web.server import sandbox_app
    if sandbox_app is not None:
        tool_set.add(PySandboxToolkit(_app=sandbox_app, _state={}))

    tool_set.add(ConfigToolkit())
    tool_set.add(UIToolkit())

    system_prompt = session.system_prompt
    if not system_prompt:
        system_prompt = (
            "You are MutBot assistant.\n"
            "- Help users with their tasks using your knowledge and available tools\n"
            "- Always respond in the user's language"
        )

    # message_metadata 配置（默认启用）
    message_metadata = config.get("message_metadata", default=True)

    agent = Agent(
        llm=client,
        tools=tool_set,
        context=AgentContext(
            message_metadata=message_metadata,
            prompts=[Message(role="system", blocks=[TextBlock(text=system_prompt)], label="base")],
            messages=messages if messages is not None else [],
        ),
        config=config,
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

    def __init__(self, config: Config | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._runtimes: dict[str, SessionRuntime] = {}
        self.config: Config = config or Config()
        # Set by server.py lifespan for per-session API recording
        self.log_dir: Path | None = None
        # Set by server.py lifespan for terminal session management
        self.terminal_manager: Any = None
        # 跨线程广播支持（Agent 线程创建 Session 时使用）
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._broadcast_fn: Any = None  # async callable(event_data: dict)
        # Dirty sessions pending persistence (thread-safe: set.add is GIL-atomic)
        self._dirty: set[str] = set()

    # --- Runtime 访问 ---

    def get_runtime(self, session_id: str) -> SessionRuntime | None:
        return self._runtimes.get(session_id)

    def get_agent_runtime(self, session_id: str) -> AgentSessionRuntime | None:
        rt = self._runtimes.get(session_id)
        return rt if isinstance(rt, AgentSessionRuntime) else None

    def get_bridge(self, session_id: str) -> AgentBridge | None:
        """获取 session 的 AgentBridge（如果 agent 正在运行）。"""
        rt = self.get_agent_runtime(session_id)
        return rt.bridge if rt else None

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
            session = Session.deserialize(data)
            self._sessions[session.id] = session
        if self._sessions:
            logger.info("Loaded %d session(s) from disk", len(self._sessions))

    def _persist(self, session: Session) -> None:
        """Save session metadata + messages to disk."""
        data = session.serialize()
        # 序列化 agent.context.messages 作为唯一消息存储
        rt = self.get_agent_runtime(session.id)
        if rt and rt.agent and rt.agent.context.messages:
            data["messages"] = [serialize_message(m) for m in rt.agent.context.messages]
        else:
            # 没有 runtime 时保留磁盘上已有的 messages（避免覆写丢失）
            existing = storage.load_session_metadata(session.id)
            if existing and "messages" in existing:
                data["messages"] = existing["messages"]
        storage.save_session_metadata(data)

    def mark_dirty(self, session_id: str) -> None:
        """标记 session 需要持久化（线程安全，任何线程可调用）。"""
        self._dirty.add(session_id)

    async def persist_dirty_loop(self) -> None:
        """定时持久化 dirty sessions（event loop 中运行）。"""
        while True:
            await asyncio.sleep(5)
            # Atomic swap under GIL — no window for mark_dirty() to lose entries
            dirty, self._dirty = self._dirty, set()
            for sid in dirty:
                session = self._sessions.get(sid)
                if session:
                    self._persist(session)

    def _load_agent_messages(self, session_id: str) -> list[Message]:
        """Load saved messages from session JSON on disk."""
        data = storage.load_session_metadata(session_id)
        if not data:
            return []

        if "messages" in data:
            return [deserialize_message(m) for m in data["messages"]]

        return []

    # --- CRUD ---

    def update(self, session_id: str, **fields: Any) -> Session | None:
        """Update session fields (title, config, status, model, …) and persist."""
        session = self._sessions.get(session_id)
        if not session:
            return None
        if "title" in fields:
            session.title = fields["title"]
        if "config" in fields:
            session.config.update(fields["config"])
        if "status" in fields:
            session.status = fields["status"]
        if "model" in fields and isinstance(session, AgentSession):
            new_model = fields["model"]
            if new_model != session.model:
                session.model = new_model
                self._swap_llm_client(session_id, session)
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist(session)
        return session

    def _swap_llm_client(self, session_id: str, session: AgentSession) -> None:
        """热切换 Agent 的 LLMClient（模型切换时调用）。"""
        rt = self.get_agent_runtime(session_id)
        if not rt or not rt.agent:
            return
        config = self.config
        try:
            new_client = create_llm_client(config, session.model, self.log_dir)
            rt.agent.llm = new_client
            session.context_window = new_client.context_window or 0
            logger.info(
                "Session %s: LLMClient swapped to model=%s",
                session_id, new_client.model,
            )
        except Exception:
            logger.exception("Session %s: failed to swap LLMClient", session_id)

    def delete(self, session_id: str) -> bool:
        """从内存中移除 session（磁盘文件保留，便于恢复）。"""
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

    def _maybe_broadcast_updated(self, session: Session) -> None:
        """如果配置了广播回调，广播 session_updated 事件。"""
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
        """更新 session 状态并广播（供 AgentBridge 等外部组件调用）。"""
        session = self._sessions.get(session_id)
        if session is None or session.status == status:
            return
        session.status = status
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist(session)
        self._maybe_broadcast_updated(session)

    # --- Agent 生命周期 ---

    def start(self, session_id: str, loop: asyncio.AbstractEventLoop) -> AgentBridge:
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

        config = self.config
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

        def _session_status_fn(status: str) -> None:
            sm.set_session_status(session_id, status)

        bridge = AgentBridge(
            session_id, agent, loop, None,
            session=session,
            persist_fn=_persist_fn,
            session_status_fn=_session_status_fn,
        )

        # 将 session 引用注入 Agent，供绑定链使用
        if not hasattr(agent, 'session'):
            agent.session = session

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

        return bridge

    async def stop(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return

        # runtime 资源清理（bridge/handler 由 SessionManager 管理）
        rt = self._runtimes.get(session_id)
        if rt and isinstance(rt, AgentSessionRuntime):
            if rt.bridge is not None:
                await rt.bridge.stop()
            _remove_session_log_handler(rt.log_handler)

        # 子类自行处理状态归位和关联资源清理
        session.on_stop(self)
        session.updated_at = datetime.now(timezone.utc).isoformat()
        # Persist final state (runtime still available so messages are included)
        self._persist(session)
        # Now safe to clear runtime
        self._runtimes.pop(session_id, None)
        logger.info("Session %s (%s): stopped", session_id, type(session).__name__)
