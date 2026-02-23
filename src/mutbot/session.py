"""Session manager — session lifecycle, Agent assembly, and persistence."""

from __future__ import annotations

import asyncio
import logging
import queue
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mutagent.agent import Agent
from mutagent.client import LLMClient
from mutagent.config import Config
from mutagent.messages import Message, ToolCall, ToolResult
from mutagent.tools import ToolSet

from mutbot import storage
from mutbot.web.agent_bridge import AgentBridge, WebUserIO
from mutbot.web.serializers import serialize_message

logger = logging.getLogger(__name__)


@dataclass
class Session:
    id: str
    workspace_id: str
    title: str
    status: str = "active"  # "active" / "ended"
    created_at: str = ""
    updated_at: str = ""
    # Runtime (not serialized)
    agent: Agent | None = field(default=None, repr=False)
    bridge: AgentBridge | None = field(default=None, repr=False)


def _session_to_dict(s: Session, include_messages: bool = False) -> dict:
    d = {
        "id": s.id,
        "workspace_id": s.workspace_id,
        "title": s.title,
        "status": s.status,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }
    if include_messages and s.agent and s.agent.messages:
        d["messages"] = [serialize_message(m) for m in s.agent.messages]
    return d


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


class SessionManager:
    """Session registry with Agent lifecycle management and persistence."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        # Set by server.py lifespan for log/API recording
        self.session_ts: str = ""
        self.log_dir: Path | None = None

    def load_from_disk(self) -> None:
        """Load all sessions from .mutbot/sessions/*.json (metadata only, no agent)."""
        for data in storage.load_all_sessions():
            session = Session(
                id=data["id"],
                workspace_id=data.get("workspace_id", ""),
                title=data.get("title", ""),
                status=data.get("status", "ended"),
                created_at=data.get("created_at", ""),
                updated_at=data.get("updated_at", ""),
            )
            self._sessions[session.id] = session
        if self._sessions:
            logger.info("Loaded %d session(s) from disk", len(self._sessions))

    def _persist(self, session: Session) -> None:
        """Save session metadata + messages to disk."""
        data = _session_to_dict(session, include_messages=True)
        storage.save_session_metadata(data)

    def _load_agent_messages(self, session_id: str) -> list[Message]:
        """Load saved messages from session JSON on disk."""
        data = storage.load_session_metadata(session_id)
        if not data or "messages" not in data:
            return []
        return [_deserialize_message(m) for m in data["messages"]]

    def record_event(self, session_id: str, event_data: dict) -> None:
        """Append an event to the session's JSONL log.

        Also persists session metadata on response_done/turn_done.
        """
        storage.append_session_event(session_id, event_data)
        etype = event_data.get("type", "")
        if etype in ("response_done", "turn_done"):
            session = self._sessions.get(session_id)
            if session:
                self._persist(session)

    def get_session_events(self, session_id: str) -> list[dict]:
        return storage.load_session_events(session_id)

    def create(self, workspace_id: str, agent_config: dict[str, Any] | None = None) -> Session:
        now = datetime.now(timezone.utc).isoformat()
        session = Session(
            id=uuid.uuid4().hex[:12],
            workspace_id=workspace_id,
            title=f"Session {len(self._sessions) + 1}",
            created_at=now,
            updated_at=now,
        )
        self._sessions[session.id] = session
        self._persist(session)
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_by_workspace(self, workspace_id: str) -> list[Session]:
        return [s for s in self._sessions.values() if s.workspace_id == workspace_id]

    def start(self, session_id: str, loop: asyncio.AbstractEventLoop, broadcast_fn=None) -> AgentBridge:
        """Assemble Agent + bridge and start the agent thread.

        If the session has persisted messages, restore them into the new Agent.

        Args:
            broadcast_fn: async callable(session_id, data) for event broadcasting.
        """
        session = self._sessions[session_id]
        if session.bridge is not None:
            return session.bridge

        # Restore messages from disk if available
        saved_messages = self._load_agent_messages(session_id)
        if saved_messages:
            logger.info("Session %s: restoring %d messages", session_id, len(saved_messages))

        agent = create_agent(
            log_dir=self.log_dir,
            session_ts=self.session_ts,
            messages=saved_messages if saved_messages else None,
        )
        session.agent = agent

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
        session.bridge = bridge
        bridge.start()
        logger.info("Session %s: agent started", session_id)
        return bridge

    async def stop(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        if session.bridge is not None:
            await session.bridge.stop()
            session.bridge = None
        # Persist final state before clearing agent
        self._persist(session)
        session.agent = None
        session.status = "ended"
        session.updated_at = datetime.now(timezone.utc).isoformat()
        # Persist ended status
        self._persist(session)
        logger.info("Session %s: stopped", session_id)
