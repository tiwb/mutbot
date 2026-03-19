"""Session 级 RPC handler — CRUD + connect/disconnect。

Declaration 子类，自动发现注册到 SessionRpc dispatcher。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mutbot.runtime import storage
from mutbot.session import SessionChannels
from mutbot.web.rpc import SessionRpc, RpcContext
from mutbot.web.serializers import session_dict, session_kind, session_type_display

logger = logging.getLogger(__name__)


class SessionOps(SessionRpc):
    """Session CRUD 和生命周期操作。"""
    namespace = "session"

    # --- connect / disconnect ---

    async def connect(self, params: dict, ctx: RpcContext) -> dict:
        session_id = params.get("session_id", "")
        if not session_id:
            raise ValueError("missing session_id")

        sm = ctx.session_manager
        cm = ctx.channel_manager
        if sm is None or cm is None:
            raise ValueError("managers not available")

        session = sm.get(session_id)
        if session is None:
            return {"error": "session not found"}

        from mutbot.web.routes import _find_client_by_ws
        client = _find_client_by_ws(ctx.sender_ws)
        if client is None:
            raise ValueError("client not found")

        channel = cm.open(client, session_id=session_id)
        SessionChannels.get_or_create(session)._channels.append(channel)
        ch_ctx = ctx.make_channel_context()

        async def _post_connect():
            await session.on_connect(channel, ch_ctx)
        ctx._post_send = _post_connect

        return {"ch": channel.ch}

    async def disconnect(self, params: dict, ctx: RpcContext) -> dict:
        session_id = params.get("session_id", "")
        ch = params.get("ch", 0)
        if not session_id or not ch:
            raise ValueError("missing session_id or ch")

        sm = ctx.session_manager
        cm = ctx.channel_manager
        if sm is None or cm is None:
            raise ValueError("managers not available")

        session = sm.get(session_id)
        channel = cm.get_channel(ch) if cm else None

        if session and channel:
            ch_ctx = ctx.make_channel_context()
            session.on_disconnect(channel, ch_ctx)
            ext = SessionChannels.get_or_create(session)
            if channel in ext._channels:
                ext._channels.remove(channel)

        if cm:
            cm.close(ch)

        return {"ok": True}

    # --- CRUD ---

    async def create(self, params: dict, ctx: RpcContext) -> dict:
        """创建 Session"""
        wm = ctx.workspace_manager
        sm = ctx.session_manager
        if not sm or not wm:
            return {"error": "managers not available"}

        ws = wm.get(ctx.workspace_id)
        if ws is None:
            return {"error": "workspace not found"}

        session_type = params.get("type", "")
        config = params.get("config")

        from mutbot.session import Session
        if not session_type:
            existing = sm.list_by_workspace(ws.id)
            if not existing:
                session_type = "mutbot.session.AgentSession"
            else:
                return {"error": "session type is required"}
        try:
            Session.get_session_class(session_type)
        except ValueError:
            return {"error": f"unknown session type: {session_type}"}

        config = config or {}
        if params.get("rows"):
            config["rows"] = params["rows"]
        if params.get("cols"):
            config["cols"] = params["cols"]
        config.setdefault("cwd", storage.STARTUP_CWD)

        session = await sm.create(ctx.workspace_id, session_type=session_type, config=config)
        ws.sessions.append(session.id)
        wm.update(ws)

        data = session_dict(session)
        await ctx.broadcast_event("session_created", data)
        return data

    async def types(self, params: dict, ctx: RpcContext) -> list[dict]:
        """返回可用的 Session 类型列表"""
        import mutobj
        from mutbot.session import Session, AgentSession

        result = []
        for cls in mutobj.discover_subclasses(Session):
            qualified = f"{cls.__module__}.{cls.__qualname__}"
            kind = session_kind(qualified)
            label, icon = session_type_display(qualified, cls)
            result.append({
                "type": qualified,
                "kind": kind,
                "label": label,
                "icon": icon,
                "is_agent": issubclass(cls, AgentSession),
            })
        return result

    async def list(self, params: dict, ctx: RpcContext) -> list[dict]:
        """列出 workspace 下的所有 Session"""
        sm = ctx.session_manager
        if not sm:
            return []
        workspace_id = params.get("workspace_id", ctx.workspace_id)
        wm = ctx.workspace_manager
        ws = wm.get(workspace_id) if wm else None
        if ws and ws.sessions:
            order = {sid: idx for idx, sid in enumerate(ws.sessions)}
            all_sessions = sm.list_by_workspace(workspace_id)
            all_sessions.sort(key=lambda s: order.get(s.id, len(order)))
            return [session_dict(s) for s in all_sessions]
        return [session_dict(s) for s in sm.list_by_workspace(workspace_id)]

    async def get(self, params: dict, ctx: RpcContext) -> dict:
        """获取单个 Session"""
        sm = ctx.session_manager
        if not sm:
            return {"error": "session_manager not available"}
        session_id = params.get("session_id", "")
        session = sm.get(session_id)
        if session is None:
            return {"error": "session not found"}
        return session_dict(session)

    async def messages(self, params: dict, ctx: RpcContext) -> dict:
        """获取 Session 的持久化消息"""
        sm = ctx.session_manager
        if not sm:
            return {"error": "session_manager not available"}
        session_id = params.get("session_id", "")
        session = sm.get(session_id)
        if session is None:
            return {"error": "session not found"}

        display_name = getattr(type(session), "display_name", "") or type(session).__name__
        agent_display: dict = {"name": display_name}
        avatar = session.config.get("avatar") if hasattr(session, "config") else None
        if avatar:
            agent_display["avatar"] = avatar

        from mutbot.web.serializers import serialize_message
        messages: list = []
        rt = sm.get_agent_runtime(session_id)
        if rt and rt.agent and rt.agent.context.messages:
            messages = [serialize_message(m) for m in rt.agent.context.messages]
        else:
            from mutbot.runtime import storage
            data = storage.load_session_metadata(session_id)
            if data:
                messages = data.get("messages", [])

        return {
            "session_id": session_id,
            "messages": messages,
            "total_tokens": getattr(session, "total_tokens", 0),
            "context_used": getattr(session, "context_used", 0),
            "context_window": getattr(session, "context_window", 0),
            "agent_display": agent_display,
        }

    async def update(self, params: dict, ctx: RpcContext) -> dict:
        """更新 Session 字段"""
        sm = ctx.session_manager
        if not sm:
            return {"error": "session_manager not available"}
        session_id = params.get("session_id", "")
        fields: dict[str, Any] = {}
        if "title" in params:
            fields["title"] = params["title"]
        if "config" in params:
            fields["config"] = params["config"]
        if "status" in params:
            fields["status"] = params["status"]
        if "model" in params:
            fields["model"] = params["model"]
        if not fields:
            return {"error": "no updatable fields"}
        session = sm.update(session_id, **fields)
        if session is None:
            return {"error": "session not found"}
        data = session_dict(session)
        await ctx.broadcast_event("session_updated", data)
        return data

    async def stop(self, params: dict, ctx: RpcContext) -> dict:
        """停止 Session"""
        sm = ctx.session_manager
        if not sm:
            return {"error": "session_manager not available"}
        session_id = params.get("session_id", "")
        await sm.stop(session_id)
        session = sm.get(session_id)
        data = session_dict(session) if session else {"session_id": session_id}
        await ctx.broadcast_event("session_updated", data)
        return {"status": session.status if session else "stopped"}

    async def delete(self, params: dict, ctx: RpcContext) -> dict:
        """删除 Session"""
        sm = ctx.session_manager
        wm = ctx.workspace_manager
        if not sm:
            return {"error": "session_manager not available"}
        session_id = params.get("session_id", "")
        from mutbot.web.routes import _close_channels_for_session
        _close_channels_for_session(session_id, "session_deleted")
        tm = ctx.terminal_manager
        if tm:
            session = sm.get(session_id)
            if session and isinstance(getattr(session, "config", None), dict):
                terminal_id = session.config.get("terminal_id")
                if terminal_id and tm.has(terminal_id):
                    await tm.notify_exit(terminal_id)
        await sm.stop(session_id)
        if not sm.delete(session_id):
            return {"error": "session not found"}
        if wm:
            ws = wm.get(ctx.workspace_id)
            if ws and session_id in ws.sessions:
                ws.sessions.remove(session_id)
                wm.update(ws)
        await ctx.broadcast_event("session_deleted", {"session_id": session_id})
        return {"status": "deleted"}

    async def delete_batch(self, params: dict, ctx: RpcContext) -> dict:
        """批量删除 sessions。"""
        sm = ctx.session_manager
        wm = ctx.workspace_manager
        if not sm:
            return {"error": "session_manager not available"}
        session_ids = params.get("session_ids", [])
        if not session_ids:
            return {"error": "no session_ids provided"}
        from mutbot.web.routes import _close_channels_for_session
        tm = ctx.terminal_manager
        ws = wm.get(ctx.workspace_id) if wm else None
        for sid in session_ids:
            _close_channels_for_session(sid, "session_deleted")
            if tm:
                session = sm.get(sid)
                if session and isinstance(getattr(session, "config", None), dict):
                    terminal_id = session.config.get("terminal_id")
                    if terminal_id and tm.has(terminal_id):
                        await tm.notify_exit(terminal_id)
            await sm.stop(sid)
            sm.delete(sid)
            if ws and sid in ws.sessions:
                ws.sessions.remove(sid)
            await ctx.broadcast_event("session_deleted", {"session_id": sid})
        if ws and wm:
            wm.update(ws)
        return {"status": "deleted", "count": len(session_ids)}

    async def restart(self, params: dict, ctx: RpcContext) -> dict:
        """重启 TerminalSession：重建 PTY。"""
        sm = ctx.session_manager
        tm = ctx.terminal_manager
        if not sm:
            return {"error": "session_manager not available"}
        session_id = params.get("session_id", "")
        session = sm.get(session_id)
        if session is None:
            return {"error": "session not found"}
        from mutbot.session import TerminalSession as _TS
        if not isinstance(session, _TS):
            return {"error": "session is not a TerminalSession"}

        # Idempotent
        existing_term_id = session.config.get("terminal_id")
        if existing_term_id and tm and tm.has(existing_term_id):
            return {"session_id": session_id, "terminal_id": existing_term_id}

        if tm:
            old_term_id = session.config.get("terminal_id")
            if old_term_id and tm.has(old_term_id):
                await tm.notify_exit(old_term_id)
                tm.kill(old_term_id)
        session.scrollback_b64 = ""
        await session.on_create(sm)
        sm._persist(session)
        new_term_id = session.config.get("terminal_id", "")

        # Re-attach via session.on_connect
        cm = ctx.channel_manager
        if cm:
            from mutbot.channel import ChannelContext as _CC
            channels = cm.get_channels_for_session(session_id)
            ch_ctx = _CC(
                workspace_id=ctx.workspace_id,
                session_manager=sm,
                terminal_manager=tm,
                event_loop=asyncio.get_running_loop(),
            )
            for channel in channels:
                await session.on_connect(channel, ch_ctx)

        data = session_dict(session)
        await ctx.broadcast_event("session_updated", data)
        return {"session_id": session_id, "terminal_id": new_term_id}

    async def run_tool(self, params: dict, ctx: RpcContext) -> dict:
        """在指定 session 中请求执行一个工具调用。"""
        sm = ctx.session_manager
        if not sm:
            return {"error": "session_manager not available"}
        session_id = params.get("session_id", "")
        tool_name = params.get("tool", "")
        tool_input = params.get("input", {})
        if not session_id or not tool_name:
            return {"error": "session_id and tool are required"}

        bridge = sm.get_bridge(session_id)
        if not bridge:
            return {"error": "session not running"}

        bridge.request_tool(tool_name, tool_input)
        return {"ok": True}

    async def run_setup(self, params: dict, ctx: RpcContext) -> dict:
        """在指定 session 上触发 Config-llm 配置工具。"""
        sm = ctx.session_manager
        if not sm:
            return {"error": "session manager not available"}

        session_id = params.get("session_id", "")
        if not session_id:
            return {"error": "session_id is required"}

        session = sm.get(session_id)
        if session is None:
            return {"error": f"session {session_id} not found"}

        loop = asyncio.get_running_loop()
        try:
            bridge = sm.start(session_id, loop)
        except Exception as exc:
            logger.exception("Failed to start bridge for setup session=%s", session_id)
            return {"error": str(exc)}

        bridge.request_tool("Config-llm")
        return {"ok": True, "session_id": session_id}
