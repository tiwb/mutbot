"""终端管理 — ptyhost WebSocket 客户端 + TerminalSession @impl。

TerminalManager 通过 WebSocket 连接 ptyhost 守护进程，
对上层接口保持不变（create/write/resize/kill/attach/detach）。

pyte 状态机已迁移到 ptyhost 进程，mutbot 侧仅负责：
- 转发 ANSI 帧给已连接的客户端
- 转发 scroll/resize 命令给 ptyhost
- 管理多客户端 attach/detach 和 resize 控制权

包含 TerminalSession 的所有 @impl：生命周期（on_create / on_stop / on_restart_cleanup）
+ Channel 通信（on_connect / on_disconnect / on_message / on_data）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, TYPE_CHECKING

from mutobj import impl

if TYPE_CHECKING:
    from mutbot.channel import Channel, ChannelContext
    from mutbot.ptyhost._client import PtyHostClient
    from mutbot.runtime.session_manager import SessionManager

# 输出回调类型：接收 ANSI 帧字节（从事件循环线程调用）
OutputCallback = Callable[[bytes], None]

# 退出回调类型：接收 exit_code（从事件循环线程调用）
ExitCallback = Callable[[int | None], None]

logger = logging.getLogger(__name__)


class TerminalManager:
    """ptyhost WebSocket 客户端包装器。

    维护 mutbot 侧的 multi-client attach/detach + 主客户端优先 resize 策略。
    PTY 操作和 pyte 渲染委托给 ptyhost 守护进程。
    """

    # 最小尺寸保护阈值（低于此值的 resize 请求直接忽略）
    MIN_ROWS = 2
    MIN_COLS = 10

    def __init__(self) -> None:
        self._client: PtyHostClient | None = None
        # term_id → {client_id: (on_output, on_exit)}
        self._connections: dict[str, dict[str, tuple[OutputCallback, ExitCallback]]] = {}
        # term_id → {client_id: (rows, cols)}
        self._client_sizes: dict[str, dict[str, tuple[int, int]]] = {}
        # 已知的终端 ID 集合
        self._known_terms: set[str] = set()
        # 用户主动锁定的客户端（None = Auto 模式）
        self._follow_me: dict[str, str | None] = {}        # {term_id: client_id | None}
        # Auto 模式下，最后打字的客户端
        self._last_input_client: dict[str, str | None] = {} # {term_id: client_id | None}
        # Per-client view：每个客户端连接拥有独立的 ptyhost view
        self._client_views: dict[str, dict[str, str]] = {}  # {term_id: {client_id: view_id}}
        # 连接锁：防止多个 on_connect 并发触发 _reconnect
        self._connect_lock: asyncio.Lock = asyncio.Lock()

    async def connect(self, host: str, port: int) -> None:
        """连接 ptyhost 守护进程。"""
        from mutbot.ptyhost._client import PtyHostClient
        client = PtyHostClient(host, port)
        client.on_frame = self._on_pty_frame
        client.on_exit = self._on_pty_exit
        client.on_disconnect = self._on_ptyhost_disconnect
        await client.connect()
        self._client = client

    async def _reconnect(self) -> None:
        """ptyhost 断开后自动重连（ensure_ptyhost 会 spawn 新实例）。"""
        async with self._connect_lock:
            if self._client and self._client.connected:
                return  # 已被其他协程连接
            from mutbot.ptyhost._bootstrap import ensure_ptyhost
            logger.info("Reconnecting to ptyhost...")
            port = await ensure_ptyhost()
            await self.connect("127.0.0.1", port)
            logger.info("Reconnected to ptyhost on port %d", port)

    async def close(self) -> None:
        """断开 ptyhost 连接（不 kill 任何终端）。"""
        if self._client:
            await self._client.close()
            self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def create(self, rows: int, cols: int, cwd: str | None = None) -> str:
        """创建终端，返回 term_id (UUID hex)。"""
        if not self._client:
            await self._reconnect()
        assert self._client, "not connected to ptyhost"
        term_id = await self._client.create(rows, cols, cwd)
        self._known_terms.add(term_id)
        return term_id

    def kill(self, term_id: str) -> None:
        """终止终端（fire-and-forget）。"""
        self._known_terms.discard(term_id)
        self._connections.pop(term_id, None)
        self._client_sizes.pop(term_id, None)
        self._follow_me.pop(term_id, None)
        self._last_input_client.pop(term_id, None)
        # 销毁所有 client view
        views = self._client_views.pop(term_id, None)
        if views and self._client:
            for vid in views.values():
                asyncio.ensure_future(self._client.destroy_view(vid))
        if self._client:
            self._client.kill_nowait(term_id)

    # ------------------------------------------------------------------
    # Connection management (attach / detach)
    # ------------------------------------------------------------------

    def attach(
        self,
        term_id: str,
        client_id: str,
        on_output: OutputCallback,
        on_exit: ExitCallback,
    ) -> None:
        """注册前端 channel 的 output/exit 回调。不改变任何控制权状态。"""
        conns = self._connections.setdefault(term_id, {})
        conns[client_id] = (on_output, on_exit)
        logger.info("Terminal %s: attached client %s (total=%d)",
                     term_id, client_id, len(conns))

    def detach(self, term_id: str, client_id: str) -> None:
        """取消注册回调 + 清理 follow_me / last_input_client。

        断开的是 follow_me 客户端 → follow_me = None（恢复 Auto）。
        断开的是 last_input_client → last_input_client = None。
        不 resize PTY，保持当前尺寸。
        """
        sizes = self._client_sizes.get(term_id)
        if sizes:
            sizes.pop(client_id, None)

        # follow_me 客户端断开 → 恢复 Auto
        if self._follow_me.get(term_id) == client_id:
            self._follow_me[term_id] = None
        # last_input_client 断开 → 清除
        if self._last_input_client.get(term_id) == client_id:
            self._last_input_client[term_id] = None

        conns = self._connections.get(term_id)
        if conns:
            conns.pop(client_id, None)
            if not conns:
                del self._connections[term_id]

        remaining = len(self._connections.get(term_id, {}))
        logger.info("Terminal %s: detached client %s (remaining=%d)",
                     term_id, client_id, remaining)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def write(self, term_id: str, data: bytes) -> None:
        """键盘输入转发到 PTY（fire-and-forget）。"""
        if self._client and term_id in self._known_terms:
            self._client.write(term_id, data)

    async def resize(
        self, term_id: str, rows: int, cols: int, client_id: str | None = None,
    ) -> tuple[int, int] | None:
        """调整终端大小，返回实际 (rows, cols)。

        resize 决策：controller 存在且 != client_id → 仅记录；
        controller 为 None 或 == client_id → 执行 resize。
        低于最小尺寸阈值的请求直接忽略，不记入 _client_sizes。
        """
        if not self._client or term_id not in self._known_terms:
            return None

        # 最小尺寸保护
        if rows < self.MIN_ROWS or cols < self.MIN_COLS:
            logger.debug(
                "resize ignored (below min): term=%s client=%s rows=%d cols=%d",
                term_id[:8], client_id[:8] if client_id else "", rows, cols,
            )
            return None

        if client_id is not None:
            sizes = self._client_sizes.setdefault(term_id, {})
            sizes[client_id] = (rows, cols)
            # 决策：controller 存在且不是该客户端 → 仅记录
            controller = self._get_resize_controller(term_id)
            if controller is not None and client_id != controller:
                return None

        # resize PTY + pyte（ptyhost 内部同步处理）
        result = await self._client.resize(term_id, rows, cols)
        if result is not None:
            logger.info(
                "resize %s: %dx%d (client=%s)",
                term_id[:8], result[1], result[0],
                client_id[:8] if client_id else "direct",
            )
        return result

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def list_terminals(self) -> list[dict[str, Any]]:
        """列出 ptyhost 中所有终端。"""
        if not self._client:
            return []
        return await self._client.list_terminals()

    def has(self, term_id: str) -> bool:
        return term_id in self._known_terms

    def _get_resize_controller(self, term_id: str) -> str | None:
        """返回当前 resize 控制者（follow_me 优先，其次 last_input_client）。"""
        fm = self._follow_me.get(term_id)
        if fm is not None:
            return fm
        return self._last_input_client.get(term_id)

    def get_follow_me(self, term_id: str) -> str | None:
        """返回当前 follow_me 客户端 ID（None = Auto 模式）。"""
        return self._follow_me.get(term_id)

    def connection_count(self, term_id: str) -> int:
        return len(self._connections.get(term_id, {}))

    async def notify_exit(self, term_id: str) -> None:
        """通知所有已 attach 的前端 channel 终端退出（用于 restart）。"""
        conns = self._connections.get(term_id)
        if not conns:
            return
        for _, (_, on_exit) in list(conns.items()):
            try:
                on_exit(None)
            except Exception:
                pass

    async def sync_from_ptyhost(self) -> set[str]:
        """从 ptyhost 同步存活终端列表。返回存活的 term_id 集合。"""
        terminals = await self.list_terminals()
        alive = set()
        for t in terminals:
            tid = t["term_id"]
            if t.get("alive", False):
                alive.add(tid)
                self._known_terms.add(tid)
        return alive

    # ------------------------------------------------------------------
    # ptyhost 回调（从 asyncio 事件循环线程调用）
    # ------------------------------------------------------------------

    def _on_pty_frame(self, term_id: str, view_id: str, frame: bytes) -> None:
        """ptyhost 推送的 ANSI 帧 → 按 view_id 路由到拥有该 view 的客户端。"""
        conns = self._connections.get(term_id)
        if not conns:
            return
        # view_id 来自二进制帧头（截断为 8 字符），用 startswith 匹配完整 view_id
        views = self._client_views.get(term_id, {})
        for client_id, vid in views.items():
            if vid.startswith(view_id):
                cb = conns.get(client_id)
                if cb:
                    try:
                        cb[0](frame)
                    except Exception:
                        logger.warning(
                            "send_binary failed for client %s on term %s",
                            client_id[:8], term_id[:8], exc_info=True,
                        )
                break

    def _on_pty_exit(self, term_id: str, exit_code: int | None) -> None:
        """ptyhost 推送的终端退出事件 → 通知前端 + 清理内部状态。"""
        conns = self._connections.get(term_id)
        if conns:
            for _, (_, on_exit) in list(conns.items()):
                try:
                    on_exit(exit_code)
                except Exception:
                    pass
        # 清理内部状态，确保 restart 时不会误判为存活
        self._known_terms.discard(term_id)
        self._connections.pop(term_id, None)
        self._client_sizes.pop(term_id, None)
        self._follow_me.pop(term_id, None)
        self._last_input_client.pop(term_id, None)
        self._client_views.pop(term_id, None)

    def _on_ptyhost_disconnect(self) -> None:
        """ptyhost 连接断开 → 通知所有终端的前端 channel 退出。"""
        logger.warning("ptyhost disconnected, notifying all terminals")
        for term_id in list(self._known_terms):
            conns = self._connections.get(term_id)
            if conns:
                for _, (_, on_exit) in list(conns.items()):
                    try:
                        on_exit(None)
                    except Exception:
                        pass
        # 清理内部状态
        self._client = None
        self._connections.clear()
        self._client_sizes.clear()
        self._known_terms.clear()
        self._follow_me.clear()
        self._last_input_client.clear()
        self._client_views.clear()


# ---------------------------------------------------------------------------
# TerminalSession @impl — 生命周期
# ---------------------------------------------------------------------------

from mutbot.session import TerminalSession


@impl(TerminalSession.on_create)
async def _terminal_on_create(self: TerminalSession, sm: SessionManager) -> None:
    """TerminalSession：通过 ptyhost 创建 PTY，设 running。"""
    tm = sm.terminal_manager
    if tm is None:
        return
    rows = self.config.get("rows", 24)
    cols = self.config.get("cols", 80)
    cwd = self.config.get("cwd", ".")

    term_id = await tm.create(rows, cols, cwd=cwd)
    self.config["terminal_id"] = term_id
    self.status = "running"


@impl(TerminalSession.on_stop)
def _terminal_on_stop(self: TerminalSession, sm: SessionManager) -> None:
    """TerminalSession：kill PTY（fire-and-forget），set stopped。"""
    tm = sm.terminal_manager
    if tm is not None and self.config:
        terminal_id = self.config.get("terminal_id")
        if terminal_id:
            tm.kill(terminal_id)
    self.status = "stopped"


@impl(TerminalSession.on_restart_cleanup)
def _terminal_on_restart_cleanup(self: TerminalSession) -> None:
    """TerminalSession：保持原状态，等首次 on_connect 时连接 ptyhost 确认。"""
    # 不改状态。首次客户端 on_connect 时懒连接 ptyhost 并 sync_from_ptyhost 确认实际状态。
    pass


# ---------------------------------------------------------------------------
# TerminalSession @impl — Channel 通信
# ---------------------------------------------------------------------------


@impl(TerminalSession.on_connect)
async def _terminal_on_connect(
    self: TerminalSession, channel: Channel, ctx: ChannelContext,
) -> None:
    """attach PTY + 请求 snapshot + 发送 ready。"""
    term_id = self.config.get("terminal_id", "")
    tm = ctx.terminal_manager
    if not tm or not term_id:
        return

    # 懒连接：如果 ptyhost 未连接，尝试连接并同步
    if not tm.connected:
        try:
            await tm._reconnect()
            await tm.sync_from_ptyhost()
        except Exception:
            logger.warning("Failed to connect to ptyhost on demand", exc_info=True)

    alive = tm.has(term_id)

    # ---- 判断终端状态，发送 ready ----
    if alive:
        channel.send_json({"type": "ready", "alive": True})
        from mutbot.web.transport import ChannelTransport
        ext = ChannelTransport.get(channel)
        client_id = ext._client.client_id if ext and ext._client else ""

        def on_output(data: bytes) -> None:
            channel.send_binary(data)

        def on_exit(exit_code: int | None) -> None:
            event: dict[str, Any] = {"type": "process_exit"}
            if exit_code is not None:
                event["exit_code"] = exit_code
            channel.send_json(event)

        # attach 客户端
        tm.attach(term_id, client_id, on_output, on_exit)

        # 注册背压恢复回调：恢复时发送 snapshot 保证画面正确
        def on_binary_resume(client: Any) -> None:
            view_id_ = tm._client_views.get(term_id, {}).get(client_id)
            if view_id_ and tm._client:
                asyncio.ensure_future(tm._client.get_snapshot(view_id_))

        if ext and ext._client:
            ext._client.on_binary_resume(on_binary_resume)

        # 为该客户端创建独立 view
        view_id: str | None = None
        if tm._client:
            try:
                view_id = await tm._client.create_view(term_id)
                if view_id:
                    tm._client_views.setdefault(term_id, {})[client_id] = view_id
            except Exception:
                logger.warning("Failed to create view for term %s", term_id[:8], exc_info=True)

        # 请求 snapshot（ANSI 帧通过 on_frame 回调异步到达，自动转发给客户端）
        if view_id and tm._client:
            try:
                await tm._client.get_snapshot(view_id)
            except Exception:
                logger.debug("Failed to get snapshot for term %s", term_id[:8], exc_info=True)

        # 向新客户端发送当前 resize 状态（新协议格式）
        follow_me = tm.get_follow_me(term_id)
        channel.send_json({
            "type": "resize_owner",
            "follow_me": follow_me,
        })
        # 发送当前 PTY 尺寸，让新客户端同步 xterm
        controller = tm._get_resize_controller(term_id)
        if controller:
            sizes = tm._client_sizes.get(term_id, {})
            if controller in sizes:
                r, c = sizes[controller]
                channel.send_json({"type": "pty_resize", "rows": r, "cols": c})
    else:
        channel.send_json({"type": "ready", "alive": False})


@impl(TerminalSession.on_disconnect)
def _terminal_on_disconnect(
    self: TerminalSession, channel: Channel, ctx: ChannelContext,
) -> None:
    """detach PTY。"""
    term_id = self.config.get("terminal_id", "")
    tm = ctx.terminal_manager
    if not tm or not term_id:
        return
    from mutbot.web.transport import ChannelTransport
    ext = ChannelTransport.get(channel)
    client_id = ext._client.client_id if ext and ext._client else ""
    if client_id:
        # 销毁该客户端的 view
        views = tm._client_views.get(term_id, {})
        view_id = views.pop(client_id, None)
        if view_id and tm._client:
            asyncio.ensure_future(tm._client.destroy_view(view_id))
        tm.detach(term_id, client_id)
        # 广播新的 resize_owner 状态（新协议格式）
        follow_me = tm.get_follow_me(term_id)
        self.broadcast_json({
            "type": "resize_owner",
            "follow_me": follow_me,
        })


@impl(TerminalSession.on_message)
async def _terminal_on_message(
    self: TerminalSession, channel: Channel, raw: dict, ctx: ChannelContext,
) -> None:
    """处理 resize / set_resize_mode / scroll。"""
    msg_type = raw.get("type")
    tm = ctx.terminal_manager
    term_id = self.config.get("terminal_id", "")

    # 获取 client_id（scroll 等命令需要路由到 per-client view）
    from mutbot.web.transport import ChannelTransport
    ext = ChannelTransport.get(channel)
    client_id = ext._client.client_id if ext and ext._client else ""

    if msg_type == "resize":
        if tm and term_id and tm.has(term_id):
            req_rows, req_cols = raw.get("rows", 24), raw.get("cols", 80)
            actual = await tm.resize(
                term_id, req_rows, req_cols, client_id=client_id,
            )
            if actual is not None:
                self.broadcast_json({"type": "pty_resize", "rows": actual[0], "cols": actual[1]})

    elif msg_type == "scroll":
        if tm and term_id and tm.has(term_id):
            lines = raw.get("lines", 0)
            view_id = tm._client_views.get(term_id, {}).get(client_id)
            if lines and view_id and tm._client:
                await tm._client.scroll(view_id, lines)
                state = await tm._client.get_scroll_state(view_id)
                if state:
                    channel.send_json({"type": "scroll_state", **state})

    elif msg_type == "scroll_to":
        if tm and term_id and tm.has(term_id):
            offset = raw.get("offset", 0)
            view_id = tm._client_views.get(term_id, {}).get(client_id)
            if view_id and tm._client:
                await tm._client.scroll_to(view_id, offset)
                state = await tm._client.get_scroll_state(view_id)
                if state:
                    channel.send_json({"type": "scroll_state", **state})

    elif msg_type == "scroll_to_bottom":
        if tm and term_id and tm.has(term_id):
            view_id = tm._client_views.get(term_id, {}).get(client_id)
            if view_id and tm._client:
                await tm._client.scroll_to_bottom(view_id)
            channel.send_json({"type": "scroll_state", "offset": 0, "total": 0, "visible": 0})

    elif msg_type == "clear_scrollback":
        if tm and term_id and tm.has(term_id) and tm._client:
            await tm._client.clear_scrollback(term_id)
            # 发送 scroll_state 给当前客户端
            view_id = tm._client_views.get(term_id, {}).get(client_id)
            if view_id:
                state = await tm._client.get_scroll_state(view_id)
                if state:
                    channel.send_json({"type": "scroll_state", **state})

    elif msg_type == "set_resize_mode":
        if tm and term_id and tm.has(term_id):
            from mutbot.web.transport import ChannelTransport
            ext = ChannelTransport.get(channel)
            client_id = ext._client.client_id if ext and ext._client else ""
            mode = raw.get("mode", "")
            if client_id and mode == "follow_me":
                # Follow Me：锁定到此客户端
                tm._follow_me[term_id] = client_id
                # 广播 resize_owner + 用该客户端尺寸 resize PTY
                self.broadcast_json({
                    "type": "resize_owner",
                    "follow_me": client_id,
                })
                sizes = tm._client_sizes.get(term_id, {})
                logger.info(
                    "pin_resize: term=%s client=%s, client_sizes=%s",
                    term_id[:8], client_id[:8],
                    {k[:8]: v for k, v in sizes.items()},
                )
                if client_id in sizes:
                    new_rows, new_cols = sizes[client_id]
                    actual = await tm.resize(term_id, new_rows, new_cols)
                    if actual is not None:
                        self.broadcast_json({"type": "pty_resize", "rows": actual[0], "cols": actual[1]})
                    logger.info(
                        "pin_resize result: requested=%dx%d actual=%s",
                        new_cols, new_rows,
                        f"{actual[1]}x{actual[0]}" if actual else "None",
                    )
                else:
                    logger.warning(
                        "pin_resize: client %s not in client_sizes, skipped resize",
                        client_id[:8],
                    )
            elif client_id and mode == "auto":
                # Auto：解除 Follow Me
                tm._follow_me[term_id] = None
                logger.info(
                    "pin_resize: auto mode, term=%s client=%s",
                    term_id[:8], client_id[:8],
                )
                self.broadcast_json({
                    "type": "resize_owner",
                    "follow_me": None,
                })


@impl(TerminalSession.on_data)
async def _terminal_on_data(
    self: TerminalSession, channel: Channel, payload: bytes, ctx: ChannelContext,
) -> None:
    """键盘输入转发到 PTY + Auto 模式下更新 last_input_client。"""
    term_id = self.config.get("terminal_id", "")
    tm = ctx.terminal_manager
    if tm and term_id and tm.has(term_id) and len(payload) > 0:
        from mutbot.web.transport import ChannelTransport
        ext = ChannelTransport.get(channel)
        client_id = ext._client.client_id if ext and ext._client else ""

        # Auto 模式下（follow_me 为 None），更新 last_input_client
        if client_id and tm._follow_me.get(term_id) is None:
            prev = tm._last_input_client.get(term_id)
            if prev != client_id:
                tm._last_input_client[term_id] = client_id
                # last_input_client 变更 → resize PTY 到该客户端尺寸
                sizes = tm._client_sizes.get(term_id, {})
                if client_id in sizes:
                    new_rows, new_cols = sizes[client_id]
                    actual = await tm.resize(term_id, new_rows, new_cols)
                    if actual is not None:
                        self.broadcast_json({"type": "pty_resize", "rows": actual[0], "cols": actual[1]})

        # 用户输入时自动回到底部
        view_id = tm._client_views.get(term_id, {}).get(client_id)
        if view_id and tm._client:
            # fire-and-forget：不等结果
            asyncio.ensure_future(tm._client.scroll_to_bottom(view_id))

        tm.write(term_id, payload)
