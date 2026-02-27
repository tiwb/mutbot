"""测试 RPC handler 层（Phase 6）

涵盖：
- session.* RPC handler 的正常路径和错误路径
- workspace.* RPC handler
- terminal.* RPC handler
- file.read RPC handler
- log.query RPC handler
- broadcast 事件推送验证
"""

from __future__ import annotations

import asyncio

import pytest

from mutbot.web.rpc import RpcContext, make_event


# ---------------------------------------------------------------------------
# 共用 Fake 对象
# ---------------------------------------------------------------------------

class FakeSession:
    def __init__(self, id="s1", workspace_id="ws_test", title="Session 1",
                 type="agent", status="active", config=None,
                 created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00"):
        self.id = id
        self.workspace_id = workspace_id
        self.title = title
        self.type = type
        self.status = status
        self.config = config or {}
        self.created_at = created_at
        self.updated_at = updated_at


class FakeSessionManager:
    def __init__(self):
        self.sessions: dict[str, FakeSession] = {}
        self.events: dict[str, list] = {}
        self.stopped: list[str] = []

    def create(self, workspace_id, session_type="agent", config=None):
        s = FakeSession(id=f"s_{len(self.sessions)+1}", workspace_id=workspace_id,
                        type=session_type, config=config or {})
        self.sessions[s.id] = s
        return s

    def get(self, session_id):
        return self.sessions.get(session_id)

    def list_by_workspace(self, workspace_id):
        return [s for s in self.sessions.values() if s.workspace_id == workspace_id]

    def get_session_events(self, session_id):
        return self.events.get(session_id, [])

    async def stop(self, session_id):
        self.stopped.append(session_id)
        s = self.sessions.get(session_id)
        if s:
            s.status = "ended"

    def delete(self, session_id):
        if session_id in self.sessions:
            del self.sessions[session_id]
            return True
        return False

    def update(self, session_id, **fields):
        s = self.sessions.get(session_id)
        if s is None:
            return None
        for k, v in fields.items():
            setattr(s, k, v)
        return s


class FakeWorkspace:
    def __init__(self, id="ws_test", name="Test", project_path="/tmp/test",
                 sessions=None, layout=None,
                 created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
                 last_accessed_at="2026-01-01T00:00:00"):
        self.id = id
        self.name = name
        self.project_path = project_path
        self.sessions = sessions or []
        self.layout = layout
        self.created_at = created_at
        self.updated_at = updated_at
        self.last_accessed_at = last_accessed_at


class FakeWorkspaceManager:
    def __init__(self):
        self.workspaces: dict[str, FakeWorkspace] = {}

    def get(self, workspace_id):
        return self.workspaces.get(workspace_id)

    def update(self, ws):
        self.workspaces[ws.id] = ws


class FakeTerminal:
    def __init__(self, id="t1", workspace_id="ws_test", rows=24, cols=80, alive=True):
        self.id = id
        self.workspace_id = workspace_id
        self.rows = rows
        self.cols = cols
        self.alive = alive


class FakeTerminalManager:
    def __init__(self):
        self.terminals: dict[str, FakeTerminal] = {}

    def create(self, workspace_id, rows, cols, cwd=""):
        t = FakeTerminal(id=f"t_{len(self.terminals)+1}", workspace_id=workspace_id,
                         rows=rows, cols=cols)
        self.terminals[t.id] = t
        return t

    def list_by_workspace(self, workspace_id):
        return [t for t in self.terminals.values() if t.workspace_id == workspace_id]

    def has(self, term_id):
        return term_id in self.terminals

    async def async_notify_exit(self, term_id):
        pass

    def kill(self, term_id):
        if term_id in self.terminals:
            del self.terminals[term_id]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _make_context(broadcasted=None, **kwargs) -> RpcContext:
    """构造测试用 RpcContext"""
    capture = broadcasted if broadcasted is not None else []

    async def capture_broadcast(data: dict) -> None:
        capture.append(data)

    wm = kwargs.pop("workspace_manager", FakeWorkspaceManager())
    sm = kwargs.pop("session_manager", FakeSessionManager())
    tm = kwargs.pop("terminal_manager", FakeTerminalManager())

    return RpcContext(
        workspace_id=kwargs.get("workspace_id", "ws_test"),
        broadcast=capture_broadcast,
        managers={
            "workspace_manager": wm,
            "session_manager": sm,
            "terminal_manager": tm,
        },
    )


async def _dispatch(method, params, ctx):
    """通过全局 workspace_rpc 分发 RPC 消息"""
    from mutbot.web.routes import workspace_rpc
    msg = {"type": "rpc", "id": "test_1", "method": method, "params": params}
    return await workspace_rpc.dispatch(msg, ctx)


# ---------------------------------------------------------------------------
# session.* handler 测试
# ---------------------------------------------------------------------------

class TestSessionHandlers:

    @pytest.mark.asyncio
    async def test_session_create_agent(self):
        wm = FakeWorkspaceManager()
        wm.workspaces["ws_test"] = FakeWorkspace()
        sm = FakeSessionManager()
        broadcasted = []
        ctx = _make_context(broadcasted, workspace_manager=wm, session_manager=sm)

        resp = await _dispatch("session.create", {"type": "mutbot.session.AgentSession"}, ctx)
        assert resp["type"] == "rpc_result"
        result = resp["result"]
        assert result["type"] == "mutbot.session.AgentSession"
        assert result["id"].startswith("s_")
        # 验证 broadcast 被调用
        assert len(broadcasted) == 1
        assert broadcasted[0]["event"] == "session_created"

    @pytest.mark.asyncio
    async def test_session_create_terminal(self):
        wm = FakeWorkspaceManager()
        wm.workspaces["ws_test"] = FakeWorkspace()
        sm = FakeSessionManager()
        tm = FakeTerminalManager()
        ctx = _make_context(workspace_manager=wm, session_manager=sm, terminal_manager=tm)

        resp = await _dispatch("session.create", {"type": "mutbot.session.TerminalSession"}, ctx)
        result = resp["result"]
        assert result["type"] == "mutbot.session.TerminalSession"
        assert result["config"]["terminal_id"].startswith("t_")

    @pytest.mark.asyncio
    async def test_session_list(self):
        sm = FakeSessionManager()
        sm.sessions["s1"] = FakeSession()
        ctx = _make_context(session_manager=sm)

        resp = await _dispatch("session.list", {"workspace_id": "ws_test"}, ctx)
        result = resp["result"]
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "s1"

    @pytest.mark.asyncio
    async def test_session_get(self):
        sm = FakeSessionManager()
        sm.sessions["s1"] = FakeSession()
        ctx = _make_context(session_manager=sm)

        resp = await _dispatch("session.get", {"session_id": "s1"}, ctx)
        assert resp["result"]["id"] == "s1"

    @pytest.mark.asyncio
    async def test_session_get_not_found(self):
        ctx = _make_context()
        resp = await _dispatch("session.get", {"session_id": "nonexistent"}, ctx)
        assert "error" in resp["result"]

    @pytest.mark.asyncio
    async def test_session_events(self):
        sm = FakeSessionManager()
        sm.sessions["s1"] = FakeSession()
        sm.events["s1"] = [{"type": "text_delta", "text": "hello"}]
        ctx = _make_context(session_manager=sm)

        resp = await _dispatch("session.events", {"session_id": "s1"}, ctx)
        result = resp["result"]
        assert result["session_id"] == "s1"
        assert len(result["events"]) == 1

    @pytest.mark.asyncio
    async def test_session_stop(self):
        sm = FakeSessionManager()
        sm.sessions["s1"] = FakeSession()
        broadcasted = []
        ctx = _make_context(broadcasted, session_manager=sm)

        resp = await _dispatch("session.stop", {"session_id": "s1"}, ctx)
        assert resp["result"]["status"] == "stopped"
        assert "s1" in sm.stopped
        # 验证 broadcast
        assert len(broadcasted) == 1
        assert broadcasted[0]["event"] == "session_updated"

    @pytest.mark.asyncio
    async def test_session_delete(self):
        sm = FakeSessionManager()
        sm.sessions["s1"] = FakeSession()
        broadcasted = []
        ctx = _make_context(broadcasted, session_manager=sm)

        resp = await _dispatch("session.delete", {"session_id": "s1"}, ctx)
        assert resp["result"]["status"] == "deleted"
        assert "s1" not in sm.sessions
        assert len(broadcasted) == 1
        assert broadcasted[0]["event"] == "session_deleted"

    @pytest.mark.asyncio
    async def test_session_delete_not_found(self):
        ctx = _make_context()
        resp = await _dispatch("session.delete", {"session_id": "nonexistent"}, ctx)
        assert "error" in resp["result"]

    @pytest.mark.asyncio
    async def test_session_update(self):
        sm = FakeSessionManager()
        sm.sessions["s1"] = FakeSession()
        broadcasted = []
        ctx = _make_context(broadcasted, session_manager=sm)

        resp = await _dispatch("session.update", {"session_id": "s1", "title": "New Title"}, ctx)
        result = resp["result"]
        assert result["title"] == "New Title"
        assert len(broadcasted) == 1
        assert broadcasted[0]["event"] == "session_updated"

    @pytest.mark.asyncio
    async def test_session_update_not_found(self):
        ctx = _make_context()
        resp = await _dispatch("session.update", {"session_id": "nonexistent", "title": "x"}, ctx)
        assert "error" in resp["result"]

    @pytest.mark.asyncio
    async def test_session_update_no_fields(self):
        sm = FakeSessionManager()
        sm.sessions["s1"] = FakeSession()
        ctx = _make_context(session_manager=sm)

        resp = await _dispatch("session.update", {"session_id": "s1"}, ctx)
        assert "error" in resp["result"]


# ---------------------------------------------------------------------------
# workspace.* handler 测试
# ---------------------------------------------------------------------------

class TestWorkspaceHandlers:

    @pytest.mark.asyncio
    async def test_workspace_get(self):
        wm = FakeWorkspaceManager()
        wm.workspaces["ws_test"] = FakeWorkspace()
        ctx = _make_context(workspace_manager=wm)

        resp = await _dispatch("workspace.get", {"workspace_id": "ws_test"}, ctx)
        result = resp["result"]
        assert result["id"] == "ws_test"
        assert result["name"] == "Test"

    @pytest.mark.asyncio
    async def test_workspace_get_not_found(self):
        ctx = _make_context()
        resp = await _dispatch("workspace.get", {"workspace_id": "nonexistent"}, ctx)
        assert "error" in resp["result"]

    @pytest.mark.asyncio
    async def test_workspace_update_layout(self):
        wm = FakeWorkspaceManager()
        wm.workspaces["ws_test"] = FakeWorkspace()
        ctx = _make_context(workspace_manager=wm)

        layout = {"global": {}, "layout": {}}
        resp = await _dispatch("workspace.update", {"workspace_id": "ws_test", "layout": layout}, ctx)
        result = resp["result"]
        assert result["layout"] == layout


# ---------------------------------------------------------------------------
# terminal.* handler 测试
# ---------------------------------------------------------------------------

class TestTerminalHandlers:

    @pytest.mark.asyncio
    async def test_terminal_create(self):
        wm = FakeWorkspaceManager()
        wm.workspaces["ws_test"] = FakeWorkspace()
        tm = FakeTerminalManager()
        broadcasted = []
        ctx = _make_context(broadcasted, workspace_manager=wm, terminal_manager=tm)

        resp = await _dispatch("terminal.create", {"rows": 30, "cols": 100}, ctx)
        result = resp["result"]
        assert result["id"].startswith("t_")
        assert result["rows"] == 30
        assert result["cols"] == 100
        assert len(broadcasted) == 1
        assert broadcasted[0]["event"] == "terminal_created"

    @pytest.mark.asyncio
    async def test_terminal_list(self):
        tm = FakeTerminalManager()
        tm.terminals["t1"] = FakeTerminal()
        ctx = _make_context(terminal_manager=tm)

        resp = await _dispatch("terminal.list", {}, ctx)
        result = resp["result"]
        assert isinstance(result, list)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_terminal_delete(self):
        tm = FakeTerminalManager()
        tm.terminals["t1"] = FakeTerminal()
        broadcasted = []
        ctx = _make_context(broadcasted, terminal_manager=tm)

        resp = await _dispatch("terminal.delete", {"term_id": "t1"}, ctx)
        assert resp["result"]["status"] == "killed"
        assert "t1" not in tm.terminals
        assert len(broadcasted) == 1
        assert broadcasted[0]["event"] == "terminal_deleted"

    @pytest.mark.asyncio
    async def test_terminal_delete_not_found(self):
        ctx = _make_context()
        resp = await _dispatch("terminal.delete", {"term_id": "nonexistent"}, ctx)
        assert "error" in resp["result"]


# ---------------------------------------------------------------------------
# log.query handler 测试
# ---------------------------------------------------------------------------

class TestLogHandlers:

    @pytest.mark.asyncio
    async def test_log_query(self):
        """log.query 在 store 不可用时返回空"""
        ctx = _make_context()
        resp = await _dispatch("log.query", {"pattern": "", "level": "DEBUG", "limit": 10}, ctx)
        result = resp["result"]
        assert result["entries"] == []
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# RPC method 注册验证
# ---------------------------------------------------------------------------

class TestMethodRegistration:

    def test_all_handlers_registered(self):
        from mutbot.web.routes import workspace_rpc
        expected = [
            "menu.query", "menu.execute",
            "session.create", "session.list", "session.get",
            "session.events", "session.stop", "session.delete", "session.update",
            "workspace.get", "workspace.update",
            "terminal.create", "terminal.list", "terminal.delete",
            "file.read", "log.query",
        ]
        for method in expected:
            assert method in workspace_rpc.methods, f"Missing handler: {method}"
