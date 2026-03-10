"""API routes — WebSocket 端点、Client 注册表、workspace 广播。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mutbot.web.rpc import RpcDispatcher, RpcContext, make_event
from mutbot.web.transport import Client, ChannelTransport, decode_varint

logger = logging.getLogger(__name__)

router = APIRouter()

# Workspace pending events: events queued before any client connects
_workspace_pending_events: dict[str, list[dict]] = {}


def queue_workspace_event(workspace_id: str, event: str, data: dict | None = None) -> None:
    """Queue an event for delivery when the first client connects to this workspace."""
    msg = {"type": "event", "event": event, "data": data or {}}
    _workspace_pending_events.setdefault(workspace_id, []).append(msg)


def _pop_pending_events(workspace_id: str) -> list[dict]:
    return _workspace_pending_events.pop(workspace_id, [])

# Client registry: client_id → Client (for reconnection matching)
_clients: dict[str, Client] = {}
# Workspace → connected Clients (for workspace-level broadcast via send buffer)
_workspace_clients: dict[str, set[Client]] = {}


def _broadcast_to_workspace(
    workspace_id: str, data: dict, *, exclude_client: Client | None = None,
) -> None:
    """广播到 workspace 的所有 Client（通过 send buffer，计入 total_sent）。"""
    clients = _workspace_clients.get(workspace_id)
    if not clients:
        return
    for c in list(clients):
        if c is exclude_client:
            continue
        c.enqueue("json", data)


def _broadcast_to_all_workspaces(data: dict) -> None:
    """广播到所有 workspace 的所有 Client。"""
    for clients in _workspace_clients.values():
        for c in list(clients):
            c.enqueue("json", data)


# Workspace RPC dispatcher
workspace_rpc = RpcDispatcher()

# App-level RPC dispatcher (用于 /ws/app 全局端点)
app_rpc = RpcDispatcher()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@router.get("/api/health")
async def health():
    """健康检查端点，返回服务状态和版本号。"""
    import mutbot
    return {"status": "ok", "version": mutbot.__version__}


# ---------------------------------------------------------------------------
# Lazy imports from server module
# ---------------------------------------------------------------------------

def _get_managers():
    from mutbot.web.server import workspace_manager, session_manager
    assert workspace_manager is not None, "workspace_manager not initialized"
    assert session_manager is not None, "session_manager not initialized"
    return workspace_manager, session_manager


def _get_terminal_manager():
    from mutbot.web.server import terminal_manager
    return terminal_manager


def _get_channel_manager():
    from mutbot.web.server import channel_manager
    return channel_manager


# ---------------------------------------------------------------------------
# App-level WebSocket RPC endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/app")
async def websocket_app(websocket: WebSocket):
    """全局 WebSocket：工作区列表、创建工作区、目录浏览。"""
    wm, sm = _get_managers()
    await websocket.accept()
    logger.info("App WS connected")

    import mutbot
    _cfg = websocket.app.state.config
    await websocket.send_json({
        "type": "event",
        "event": "welcome",
        "data": {
            "version": mutbot.__version__,
            "setup_required": not bool(_cfg.get("providers")),
        },
    })

    async def broadcast(data: dict) -> None:
        pass

    context = RpcContext(
        workspace_id="",
        broadcast=broadcast,
        workspace_manager=wm,
        session_manager=sm,
        config=_cfg,
    )

    # 注册 App RPC handler
    from mutbot.web.rpc_app import register_app_rpc
    register_app_rpc(app_rpc)

    try:
        while True:
            raw = await websocket.receive_json()
            response = await app_rpc.dispatch(raw, context)
            if response is not None:
                await websocket.send_json(response)
    except WebSocketDisconnect:
        logger.info("App WS disconnected")
    except Exception:
        logger.exception("App WS error")


# ---------------------------------------------------------------------------
# Workspace WebSocket RPC endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/workspace/{workspace_id}")
async def websocket_workspace(websocket: WebSocket, workspace_id: str):
    """统一 Workspace WebSocket：承载 RPC 调用、事件推送和 Channel 多路复用。

    支持 Text Frame（JSON）和 Binary Frame 混合传输。
    通过 client_id + last_seq 支持断线重连时的消息恢复。
    """
    wm, sm = _get_managers()
    ws = wm.get(workspace_id)
    if ws is None:
        await websocket.close(code=4004, reason="workspace not found")
        return

    wm.touch_accessed(ws)

    # --- 解析重连参数 ---
    client_id = websocket.query_params.get("client_id", "")
    last_seq_str = websocket.query_params.get("last_seq")
    last_seq = int(last_seq_str) if last_seq_str is not None else None

    await websocket.accept()

    loop = asyncio.get_running_loop()
    cm = _get_channel_manager()

    # --- 确保 SessionManager 能从 Agent 线程广播事件 ---
    if sm and not sm._broadcast_fn:
        async def _sm_broadcast(ws_id: str, data: dict) -> None:
            _broadcast_to_workspace(ws_id, data)
        sm.set_broadcast(loop, _sm_broadcast)

    # --- Client 匹配 / 创建 ---
    resumed = False
    client: Client | None = None

    if client_id and last_seq is not None:
        client = _clients.get(client_id)
        if client and client.state == "buffering" and client.workspace_id == workspace_id:
            if client.resume(websocket, last_seq):
                resumed = True
                logger.info(
                    "Workspace WS resumed: client=%s, last_seq=%d",
                    client_id, last_seq,
                )
            else:
                client.reset_for_fresh_connection(websocket)
                logger.info(
                    "Workspace WS full reconnect (seq out of range): client=%s",
                    client_id,
                )
        else:
            client = None

    if client is None:
        if not client_id:
            import uuid
            client_id = str(uuid.uuid4())
        client = Client(client_id, workspace_id, websocket, loop=loop)
        client.start()

        def _on_client_expire(c: Client) -> None:
            closed_channels = cm.close_all_for_client(c)
            for channel in closed_channels:
                if channel.session_id and sm:
                    session = sm.get(channel.session_id)
                    if session:
                        from mutbot.session import SessionChannels
                        from mutbot.channel import ChannelContext
                        ext = SessionChannels.get_or_create(session)
                        if channel in ext._channels:
                            ext._channels.remove(channel)
                        try:
                            ch_ctx = ChannelContext(
                                workspace_id=workspace_id,
                                session_manager=sm,
                                terminal_manager=_get_terminal_manager(),
                                event_loop=loop,
                            )
                            session.on_disconnect(channel, ch_ctx)
                        except Exception:
                            logger.debug("on_disconnect error during expire", exc_info=True)
            _clients.pop(c.client_id, None)
            ws_clients = _workspace_clients.get(c.workspace_id)
            if ws_clients:
                ws_clients.discard(c)
                if not ws_clients:
                    del _workspace_clients[c.workspace_id]

        client.on_expire(_on_client_expire)
        _clients[client_id] = client
        logger.info("Workspace WS connected: client=%s, workspace=%s", client_id, workspace_id)

    # --- 发送 welcome ---
    welcome: dict[str, Any] = {"type": "welcome", "resumed": resumed}
    if resumed:
        assert last_seq is not None
        welcome["last_seq"] = client.recv_count
        replay_msgs = client.get_replay_messages(last_seq)
        try:
            await websocket.send_json(welcome)
            for frame_type, data in replay_msgs:
                if frame_type == "json":
                    await websocket.send_json(data)
                elif isinstance(data, bytes):
                    await websocket.send_bytes(data)
        except Exception:
            logger.exception("Failed to send welcome/replay")
            client.enter_buffering()
            return
    else:
        try:
            await websocket.send_json(welcome)
        except Exception:
            logger.exception("Failed to send welcome")
            return

    # --- 注册到 workspace clients 索引 ---
    _workspace_clients.setdefault(workspace_id, set()).add(client)

    if not resumed:
        client.enqueue("json", make_event("config_changed", {"reason": "connect"}))

    if not resumed:
        for event in _pop_pending_events(workspace_id):
            client.enqueue("json", event)

    # --- RPC context ---
    async def broadcast(data: dict) -> None:
        _broadcast_to_workspace(workspace_id, data)

    context = RpcContext(
        workspace_id=workspace_id,
        broadcast=broadcast,
        workspace_manager=wm,
        session_manager=sm,
        terminal_manager=_get_terminal_manager(),
        channel_manager=cm,
        config=websocket.app.state.config,
        event_loop=asyncio.get_running_loop(),
        sender_ws=websocket,
    )

    # --- 注册 RPC handler ---
    from mutbot.web.rpc_session import register_session_rpc
    from mutbot.web.rpc_workspace import register_workspace_rpc
    register_session_rpc(workspace_rpc)
    register_workspace_rpc(workspace_rpc)

    # --- 消息循环 ---
    try:
        while True:
            msg = await websocket.receive()
            ws_type = msg.get("type", "")

            if ws_type == "websocket.receive":
                if "text" in msg:
                    # --- JSON (Text Frame) ---
                    import json as _json
                    try:
                        raw = _json.loads(msg["text"])
                    except Exception:
                        logger.warning("Invalid JSON in WS frame", exc_info=True)
                        continue

                    msg_type = raw.get("type", "")

                    if msg_type == "ack":
                        client.on_peer_ack(raw.get("ack", 0))
                        client.on_control_received()
                        continue

                    client.on_content_received()

                    ch = raw.get("ch", 0)
                    if ch == 0:
                        # Workspace 级消息 → RPC dispatch
                        context._post_send = None
                        response = await workspace_rpc.dispatch(raw, context)
                        if response is not None:
                            client.enqueue("json", response)
                        if context._post_send is not None:
                            context._post_send()
                    else:
                        # Channel 消息 → session.on_message
                        channel = cm.get_channel(ch)
                        if channel and channel.session_id:
                            session = sm.get(channel.session_id)
                            if session:
                                ch_ctx = context.make_channel_context()
                                await session.on_message(channel, raw, ch_ctx)

                elif "bytes" in msg:
                    # --- Binary Frame ---
                    data = msg["bytes"]
                    if not data:
                        continue
                    client.on_content_received()
                    try:
                        ch_id, consumed = decode_varint(data)
                    except ValueError:
                        logger.warning("Invalid varint in binary frame", exc_info=True)
                        continue
                    channel = cm.get_channel(ch_id)
                    if channel and channel.session_id:
                        session = sm.get(channel.session_id)
                        if session:
                            ch_ctx = context.make_channel_context()
                            await session.on_data(channel, data[consumed:], ch_ctx)

            elif ws_type == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        logger.info("Workspace WS disconnected: client=%s", client_id)
    except Exception:
        logger.exception("Workspace WS error: client=%s", client_id)
    finally:
        ws_clients = _workspace_clients.get(workspace_id)
        if ws_clients:
            ws_clients.discard(client)
            if not ws_clients:
                del _workspace_clients[workspace_id]
        client.enter_buffering()


# ---------------------------------------------------------------------------
# Shared helpers (imported by rpc_*.py modules)
# ---------------------------------------------------------------------------

def _find_client_by_ws(ws) -> Client | None:
    """查找拥有指定 WebSocket 的 Client。"""
    for client in _clients.values():
        if client.ws is ws:
            return client
    return None


def _close_channels_for_session(session_id: str, reason: str) -> None:
    """关闭指定 session 的所有 channel 并推送 channel.closed 事件。"""
    cm = _get_channel_manager()
    if cm is None:
        return
    _, sm = _get_managers()
    channels = cm.get_channels_for_session(session_id)
    for channel in channels:
        if sm:
            session = sm.get(session_id)
            if session:
                from mutbot.session import SessionChannels
                ext = SessionChannels.get_or_create(session)
                if channel in ext._channels:
                    ext._channels.remove(channel)
                try:
                    from mutbot.channel import ChannelContext as _CC
                    ch_ctx = _CC(
                        workspace_id=session.workspace_id,
                        session_manager=sm,
                        terminal_manager=_get_terminal_manager(),
                        event_loop=asyncio.get_running_loop(),
                    )
                    session.on_disconnect(channel, ch_ctx)
                except Exception:
                    logger.debug("on_disconnect error during close_channels", exc_info=True)
        ext_t = ChannelTransport.get(channel)
        if ext_t and ext_t._client:
            ext_t._client.enqueue("json", {
                "type": "event",
                "event": "channel.closed",
                "closed_ch": channel.ch,
                "reason": reason,
            })
        cm.close(channel.ch)
