"""Session manager — session lifecycle and Agent assembly."""

from __future__ import annotations

import asyncio
import logging
import queue
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from mutagent.agent import Agent
from mutagent.client import LLMClient
from mutagent.config import Config
from mutagent.tools import ToolSet

from mutbot.web.agent_bridge import AgentBridge, WebUserIO

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


def create_agent(agent_config: dict[str, Any] | None = None) -> Agent:
    """Assemble a mutagent Agent from configuration.

    Loads Config from the standard location, builds LLMClient + ToolSet,
    and returns a ready-to-run Agent.
    """
    import importlib
    import os
    import sys
    from pathlib import Path

    from mutagent.main import App
    from mutagent.toolkits.module_toolkit import ModuleToolkit
    from mutagent.toolkits.log_toolkit import LogToolkit
    from mutagent.runtime.module_manager import ModuleManager
    from mutagent.runtime.log_store import LogStore, LogStoreHandler

    # Load config
    config = Config.load(".mutagent/config.json")

    # Set env vars
    for key, value in config.get("env", {}).items():
        os.environ[key] = value

    # Setup sys.path
    for mutagent_dir in [
        str(Path.home() / ".mutagent"),
        str(Path.cwd() / ".mutagent"),
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
    search_dirs = [Path.home() / ".mutagent", Path.cwd() / ".mutagent"]
    module_manager = ModuleManager(search_dirs=search_dirs)
    module_tools = ModuleToolkit(module_manager=module_manager)

    log_store = LogStore()
    log_tools = LogToolkit(log_store=log_store)

    tool_set = ToolSet(auto_discover=True)
    tool_set.add(module_tools)
    tool_set.add(log_tools)

    client = LLMClient(
        model=model.get("model_id", ""),
        api_key=model.get("auth_token", ""),
        base_url=model.get("base_url", ""),
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
        messages=[],
    )
    tool_set.agent = agent
    return agent


class SessionManager:
    """In-memory session registry with Agent lifecycle management."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

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
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_by_workspace(self, workspace_id: str) -> list[Session]:
        return [s for s in self._sessions.values() if s.workspace_id == workspace_id]

    def start(self, session_id: str, loop: asyncio.AbstractEventLoop, broadcast_fn=None) -> AgentBridge:
        """Assemble Agent + bridge and start the agent thread.

        Args:
            broadcast_fn: async callable(session_id, data) for event broadcasting.
        """
        session = self._sessions[session_id]
        if session.bridge is not None:
            return session.bridge

        agent = create_agent()
        session.agent = agent

        input_q: queue.Queue = queue.Queue()

        def _event_callback(data):
            loop.call_soon_threadsafe(bridge._event_queue.put_nowait, data)

        web_userio = WebUserIO(input_queue=input_q, event_callback=_event_callback)
        bridge = AgentBridge(session_id, agent, web_userio, loop, broadcast_fn)
        session.bridge = bridge
        bridge.start()
        logger.info("Session %s: agent started", session_id)
        return bridge

    def stop(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        if session.bridge is not None:
            session.bridge.stop()
            session.bridge = None
        session.agent = None
        session.status = "ended"
        session.updated_at = datetime.now(timezone.utc).isoformat()
        logger.info("Session %s: stopped", session_id)
