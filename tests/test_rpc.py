"""测试 WebSocket RPC 框架（Phase 3）

涵盖：
- RpcDispatcher 消息分发
- handler 注册（装饰器 + 编程式）
- 正常响应 / 错误响应 / 未知方法
- RpcContext 传递
- make_event 辅助函数
"""

from __future__ import annotations

import asyncio

import pytest

from mutbot.web.rpc import (
    ERR_INTERNAL,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    RpcContext,
    RpcDispatcher,
    make_event,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _make_context(**kwargs) -> RpcContext:
    """构造一个最小化的 RpcContext"""
    async def noop_broadcast(data: dict) -> None:
        pass
    return RpcContext(
        workspace_id=kwargs.get("workspace_id", "ws_test"),
        broadcast=kwargs.get("broadcast", noop_broadcast),
        managers=kwargs.get("managers", {}),
    )


# ---------------------------------------------------------------------------
# Handler 注册
# ---------------------------------------------------------------------------

class TestHandlerRegistration:

    def test_method_decorator(self):
        d = RpcDispatcher()

        @d.method("echo")
        async def echo(params, ctx):
            return params

        assert "echo" in d.methods

    def test_register_programmatic(self):
        d = RpcDispatcher()

        async def handler(params, ctx):
            return "ok"

        d.register("test.method", handler)
        assert "test.method" in d.methods

    def test_methods_list(self):
        d = RpcDispatcher()

        @d.method("a")
        async def a(p, c): ...

        @d.method("b")
        async def b(p, c): ...

        assert set(d.methods) == {"a", "b"}


# ---------------------------------------------------------------------------
# 消息分发
# ---------------------------------------------------------------------------

class TestDispatch:

    @pytest.mark.asyncio
    async def test_successful_call(self):
        d = RpcDispatcher()

        @d.method("echo")
        async def echo(params, ctx):
            return {"echoed": params.get("msg")}

        msg = {"type": "rpc", "id": "req_1", "method": "echo", "params": {"msg": "hello"}}
        ctx = _make_context()
        resp = await d.dispatch(msg, ctx)

        assert resp == {
            "type": "rpc_result",
            "id": "req_1",
            "result": {"echoed": "hello"},
        }

    @pytest.mark.asyncio
    async def test_method_not_found(self):
        d = RpcDispatcher()
        msg = {"type": "rpc", "id": "req_2", "method": "nonexistent", "params": {}}
        ctx = _make_context()
        resp = await d.dispatch(msg, ctx)

        assert resp["type"] == "rpc_error"
        assert resp["id"] == "req_2"
        assert resp["error"]["code"] == ERR_METHOD_NOT_FOUND
        assert "nonexistent" in resp["error"]["message"]

    @pytest.mark.asyncio
    async def test_missing_method_name(self):
        d = RpcDispatcher()
        msg = {"type": "rpc", "id": "req_3", "method": "", "params": {}}
        ctx = _make_context()
        resp = await d.dispatch(msg, ctx)

        assert resp["type"] == "rpc_error"
        assert resp["error"]["code"] == ERR_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_handler_exception(self):
        d = RpcDispatcher()

        @d.method("fail")
        async def fail(params, ctx):
            raise RuntimeError("boom")

        msg = {"type": "rpc", "id": "req_4", "method": "fail", "params": {}}
        ctx = _make_context()
        resp = await d.dispatch(msg, ctx)

        assert resp["type"] == "rpc_error"
        assert resp["id"] == "req_4"
        assert resp["error"]["code"] == ERR_INTERNAL
        assert "boom" in resp["error"]["message"]

    @pytest.mark.asyncio
    async def test_non_rpc_message_returns_none(self):
        d = RpcDispatcher()
        msg = {"type": "log", "level": "debug", "message": "test"}
        ctx = _make_context()
        resp = await d.dispatch(msg, ctx)
        assert resp is None

    @pytest.mark.asyncio
    async def test_default_params_is_empty_dict(self):
        d = RpcDispatcher()

        @d.method("check")
        async def check(params, ctx):
            return {"has_params": len(params) > 0}

        # 不传 params 字段
        msg = {"type": "rpc", "id": "req_5", "method": "check"}
        ctx = _make_context()
        resp = await d.dispatch(msg, ctx)

        assert resp["result"] == {"has_params": False}


# ---------------------------------------------------------------------------
# RpcContext 传递
# ---------------------------------------------------------------------------

class TestRpcContext:

    @pytest.mark.asyncio
    async def test_context_workspace_id(self):
        d = RpcDispatcher()

        @d.method("get_ws")
        async def get_ws(params, ctx: RpcContext):
            return {"workspace_id": ctx.workspace_id}

        msg = {"type": "rpc", "id": "r1", "method": "get_ws", "params": {}}
        ctx = _make_context(workspace_id="ws_42")
        resp = await d.dispatch(msg, ctx)

        assert resp["result"]["workspace_id"] == "ws_42"

    @pytest.mark.asyncio
    async def test_context_managers(self):
        d = RpcDispatcher()

        @d.method("get_mgr")
        async def get_mgr(params, ctx: RpcContext):
            return {"has_sm": "session_manager" in ctx.managers}

        msg = {"type": "rpc", "id": "r2", "method": "get_mgr", "params": {}}
        ctx = _make_context(managers={"session_manager": object()})
        resp = await d.dispatch(msg, ctx)

        assert resp["result"]["has_sm"] is True

    @pytest.mark.asyncio
    async def test_context_broadcast(self):
        """handler 可以通过 ctx.broadcast 推送事件"""
        d = RpcDispatcher()
        broadcasted = []

        async def capture_broadcast(data: dict) -> None:
            broadcasted.append(data)

        @d.method("notify")
        async def notify(params, ctx: RpcContext):
            await ctx.broadcast(make_event("test_event", {"key": "val"}))
            return "ok"

        msg = {"type": "rpc", "id": "r3", "method": "notify", "params": {}}
        ctx = _make_context(broadcast=capture_broadcast)
        resp = await d.dispatch(msg, ctx)

        assert resp["result"] == "ok"
        assert len(broadcasted) == 1
        assert broadcasted[0]["type"] == "event"
        assert broadcasted[0]["event"] == "test_event"
        assert broadcasted[0]["data"] == {"key": "val"}


# ---------------------------------------------------------------------------
# make_event 辅助
# ---------------------------------------------------------------------------

class TestMakeEvent:

    def test_basic_event(self):
        e = make_event("session_created", {"session_id": "s1"})
        assert e == {
            "type": "event",
            "event": "session_created",
            "data": {"session_id": "s1"},
        }

    def test_event_default_data(self):
        e = make_event("ping")
        assert e == {"type": "event", "event": "ping", "data": {}}

    def test_event_none_data(self):
        e = make_event("tick", None)
        assert e["data"] == {}


# ---------------------------------------------------------------------------
# workspace_rpc 全局实例
# ---------------------------------------------------------------------------

class TestWorkspaceRpcInstance:

    def test_workspace_rpc_importable(self):
        from mutbot.web.routes import workspace_rpc
        assert isinstance(workspace_rpc, RpcDispatcher)

    def test_workspace_connection_manager_importable(self):
        from mutbot.web.routes import workspace_connection_manager
        from mutbot.web.connection import ConnectionManager
        assert isinstance(workspace_connection_manager, ConnectionManager)
