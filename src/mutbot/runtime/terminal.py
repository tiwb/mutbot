"""终端管理 — ptyhost WebSocket 客户端 + TerminalSession @impl。

TerminalManager 通过 WebSocket 连接 ptyhost 守护进程，
对上层接口保持不变（create/write/resize/kill/get_scrollback/attach/detach）。

包含 TerminalSession 的所有 @impl：生命周期（on_create / on_stop / on_restart_cleanup）
+ Channel 通信（on_connect / on_disconnect / on_message / on_data）。
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any, Callable, TYPE_CHECKING

from mutobj import impl

if TYPE_CHECKING:
    from mutbot.channel import Channel, ChannelContext
    from mutbot.ptyhost._client import PtyHostClient
    from mutbot.runtime.session_manager import SessionManager

# 输出回调类型：接收原始 PTY 输出字节（从事件循环线程调用）
OutputCallback = Callable[[bytes], None]

# 退出回调类型：接收 exit_code（从事件循环线程调用）
ExitCallback = Callable[[int | None], None]

logger = logging.getLogger(__name__)

_CSI_QUERY_RE = re.compile(rb"\x1b\[(?:[>=]?0?c|[56]n)")
_CLEAR_SCREEN = b"\x1b[0m\x1b[2J\x1b[H"


def _strip_replay_queries(data: bytes) -> bytes:
    return _CSI_QUERY_RE.sub(b"", data)


class TerminalManager:
    """ptyhost WebSocket 客户端包装器。

    维护 mutbot 侧的 multi-client attach/detach + 主客户端优先 resize 策略。
    PTY 操作委托给 ptyhost 守护进程。
    """

    def __init__(self) -> None:
        self._client: PtyHostClient | None = None
        # term_id → {client_id: (on_output, on_exit)}
        self._connections: dict[str, dict[str, tuple[OutputCallback, ExitCallback]]] = {}
        # term_id → {client_id: (rows, cols)}
        self._client_sizes: dict[str, dict[str, tuple[int, int]]] = {}
        # 已知的终端 ID 集合
        self._known_terms: set[str] = set()
        # 主客户端控制权（resize 跟随主客户端）
        self._primary_client: dict[str, str] = {}     # {term_id: client_id}
        self._primary_locked: dict[str, bool] = {}    # {term_id: locked}

    async def connect(self, host: str, port: int) -> None:
        """连接 ptyhost 守护进程。"""
        from mutbot.ptyhost._client import PtyHostClient
        client = PtyHostClient(host, port)
        client.on_output = self._on_pty_output
        client.on_exit = self._on_pty_exit
        client.on_disconnect = self._on_ptyhost_disconnect
        await client.connect()
        self._client = client

    async def _reconnect(self) -> None:
        """ptyhost 断开后自动重连（ensure_ptyhost 会 spawn 新实例）。"""
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
        self._primary_client.pop(term_id, None)
        self._primary_locked.pop(term_id, None)
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
    ) -> bool:
        """注册前端 channel 的 output/exit 回调。

        返回 True 如果该客户端成为了主客户端（首个连接）。
        """
        conns = self._connections.setdefault(term_id, {})
        conns[client_id] = (on_output, on_exit)
        became_primary = False
        if term_id not in self._primary_client:
            self._primary_client[term_id] = client_id
            self._primary_locked[term_id] = False
            became_primary = True
        logger.info("Terminal %s: attached client %s (total=%d, primary=%s)",
                     term_id, client_id, len(conns), became_primary)
        return became_primary

    def detach(self, term_id: str, client_id: str) -> tuple[int, int] | None:
        """取消注册回调。

        如果断开的是主客户端，选新主并用新主尺寸 resize，返回新尺寸。
        如果断开的不是主客户端，仅清理，返回 None。
        """
        result: tuple[int, int] | None = None
        sizes = self._client_sizes.get(term_id)
        if sizes:
            sizes.pop(client_id, None)

        was_primary = self._primary_client.get(term_id) == client_id
        if was_primary:
            # 主客户端断开 → 解锁 + 选新主
            self._primary_locked.pop(term_id, None)
            self._primary_client.pop(term_id, None)
            # 从存活连接中选新主
            conns = self._connections.get(term_id)
            if conns and len(conns) > 1:
                # 排除即将移除的 client_id
                remaining = [cid for cid in conns if cid != client_id]
                if remaining:
                    new_primary = remaining[0]
                    self._primary_client[term_id] = new_primary
                    self._primary_locked[term_id] = False
                    # 用新主客户端的尺寸 resize
                    if sizes and new_primary in sizes:
                        eff_rows, eff_cols = sizes[new_primary]
                        result = (eff_rows, eff_cols)
                        if self._client:
                            self._client.resize_nowait(term_id, eff_rows, eff_cols)

        conns = self._connections.get(term_id)
        if conns:
            conns.pop(client_id, None)
            if not conns:
                del self._connections[term_id]

        remaining = len(self._connections.get(term_id, {}))
        logger.info("Terminal %s: detached client %s (remaining=%d, was_primary=%s)",
                     term_id, client_id, remaining, was_primary)
        return result

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def write(self, term_id: str, data: bytes) -> None:
        """键盘输入转发到 PTY（fire-and-forget）。"""
        if self._client and term_id in self._known_terms:
            self._client.write(term_id, data)

    # 最小尺寸保护阈值（低于此值的 resize 请求直接忽略）
    MIN_ROWS = 2
    MIN_COLS = 10

    async def resize(
        self, term_id: str, rows: int, cols: int, client_id: str | None = None,
    ) -> tuple[int, int] | None:
        """调整终端大小，返回实际 (rows, cols)。

        只有主客户端的 resize 生效；非主客户端的 resize 只记录 _client_sizes。
        低于最小尺寸阈值的请求直接忽略。
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
            # 只有主客户端的 resize 生效
            primary = self._primary_client.get(term_id)
            if primary and client_id != primary:
                return None
            eff_rows, eff_cols = rows, cols
        else:
            eff_rows, eff_cols = rows, cols

        return await self._client.resize(term_id, eff_rows, eff_cols)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def get_scrollback(self, term_id: str) -> bytes:
        """获取 scrollback 数据。"""
        if not self._client:
            return b""
        return await self._client.get_scrollback(term_id)

    async def list_terminals(self) -> list[dict[str, Any]]:
        """列出 ptyhost 中所有终端。"""
        if not self._client:
            return []
        return await self._client.list_terminals()

    def has(self, term_id: str) -> bool:
        return term_id in self._known_terms

    def get_primary_info(self, term_id: str) -> tuple[str, bool]:
        """返回 (primary_client_id, locked)。"""
        return (
            self._primary_client.get(term_id, ""),
            self._primary_locked.get(term_id, False),
        )

    def try_set_primary(
        self, term_id: str, client_id: str, *, lock: bool | None = None,
    ) -> bool:
        """尝试设为主客户端。locked 时自动模式下不切换。

        lock=None 表示自动模式（输入触发），仅在未锁定时切换。
        lock=True/False 表示手动操作（claim_resize）。
        返回 True 表示主客户端发生了变化。
        """
        current = self._primary_client.get(term_id)
        if lock is None:
            # 自动模式：已锁定时不切换
            if self._primary_locked.get(term_id, False):
                return False
            if current == client_id:
                return False
            self._primary_client[term_id] = client_id
            return True
        else:
            # 手动操作
            self._primary_client[term_id] = client_id
            self._primary_locked[term_id] = lock
            changed = current != client_id or self._primary_locked.get(term_id) != lock
            return changed

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

    def _on_pty_output(self, term_id: str, data: bytes) -> None:
        """ptyhost 推送的 PTY 输出 → 广播到已 attach 的前端 channel。"""
        conns = self._connections.get(term_id)
        if not conns:
            return
        dead: list[str] = []
        for client_id, (on_output, _) in list(conns.items()):
            try:
                on_output(data)
            except Exception:
                dead.append(client_id)
        for client_id in dead:
            conns.pop(client_id, None)

    def _on_pty_exit(self, term_id: str, exit_code: int | None) -> None:
        """ptyhost 推送的终端退出事件 → 通知已 attach 的前端 channel。"""
        conns = self._connections.get(term_id)
        if not conns:
            return
        for _, (_, on_exit) in list(conns.items()):
            try:
                on_exit(exit_code)
            except Exception:
                pass

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
        self._primary_client.clear()
        self._primary_locked.clear()


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
    """attach PTY + scrollback replay + 发送 ready。"""
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

    # ---- 发送 scrollback（始终先清屏） ----
    if alive:
        scrollback = await tm.get_scrollback(term_id)
        scrollback = _strip_replay_queries(scrollback) if scrollback else b""
        channel.send_binary(_CLEAR_SCREEN + scrollback)
    elif self.scrollback_b64:
        try:
            scrollback_data = base64.b64decode(self.scrollback_b64)
            scrollback_data = _strip_replay_queries(scrollback_data) if scrollback_data else b""
            channel.send_binary(_CLEAR_SCREEN + scrollback_data)
        except Exception:
            logger.warning("scrollback decode failed", exc_info=True)
            channel.send_binary(_CLEAR_SCREEN)
    else:
        channel.send_binary(_CLEAR_SCREEN)

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

        tm.attach(term_id, client_id, on_output, on_exit)
        # 向新客户端发送当前 resize_owner 状态 + PTY 尺寸
        primary, locked = tm.get_primary_info(term_id)
        if primary:
            channel.send_json({
                "type": "resize_owner",
                "client_id": primary,
                "locked": locked,
            })
            # 发送当前 PTY 尺寸，让新客户端同步 xterm
            sizes = tm._client_sizes.get(term_id, {})
            if primary in sizes:
                r, c = sizes[primary]
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
        new_size = tm.detach(term_id, client_id)
        if new_size is not None:
            self.broadcast_json({"type": "pty_resize", "rows": new_size[0], "cols": new_size[1]})
        # 广播新的 resize_owner 状态
        primary, locked = tm.get_primary_info(term_id)
        self.broadcast_json({
            "type": "resize_owner",
            "client_id": primary,
            "locked": locked,
        })


@impl(TerminalSession.on_message)
async def _terminal_on_message(
    self: TerminalSession, channel: Channel, raw: dict, ctx: ChannelContext,
) -> None:
    """处理 resize / claim_resize。"""
    msg_type = raw.get("type")
    tm = ctx.terminal_manager
    term_id = self.config.get("terminal_id", "")

    if msg_type == "resize":
        if tm and term_id and tm.has(term_id):
            from mutbot.web.transport import ChannelTransport
            ext = ChannelTransport.get(channel)
            client_id = ext._client.client_id if ext and ext._client else ""
            req_rows, req_cols = raw.get("rows", 24), raw.get("cols", 80)
            actual = await tm.resize(
                term_id, req_rows, req_cols, client_id=client_id,
            )
            if actual is not None:
                self.broadcast_json({"type": "pty_resize", "rows": actual[0], "cols": actual[1]})

    elif msg_type == "claim_resize":
        if tm and term_id and tm.has(term_id):
            from mutbot.web.transport import ChannelTransport
            ext = ChannelTransport.get(channel)
            client_id = ext._client.client_id if ext and ext._client else ""
            lock = bool(raw.get("lock", False))
            if client_id:
                if lock:
                    # 锁定到此客户端
                    tm.try_set_primary(term_id, client_id, lock=True)
                else:
                    # 解锁回自动模式
                    tm.try_set_primary(term_id, client_id, lock=False)
                # 广播新状态
                primary, locked = tm.get_primary_info(term_id)
                self.broadcast_json({
                    "type": "resize_owner",
                    "client_id": primary,
                    "locked": locked,
                })
                # 用该客户端尺寸 resize
                sizes = tm._client_sizes.get(term_id, {})
                if client_id in sizes:
                    new_rows, new_cols = sizes[client_id]
                    actual = await tm.resize(term_id, new_rows, new_cols)
                    if actual is not None:
                        self.broadcast_json({"type": "pty_resize", "rows": actual[0], "cols": actual[1]})


@impl(TerminalSession.on_data)
async def _terminal_on_data(
    self: TerminalSession, channel: Channel, payload: bytes, ctx: ChannelContext,
) -> None:
    """键盘输入转发到 PTY + 自动模式下切换主客户端。"""
    term_id = self.config.get("terminal_id", "")
    tm = ctx.terminal_manager
    if tm and term_id and tm.has(term_id) and len(payload) > 0:
        # 输入触发主客户端自动切换
        from mutbot.web.transport import ChannelTransport
        ext = ChannelTransport.get(channel)
        client_id = ext._client.client_id if ext and ext._client else ""
        if client_id and tm.try_set_primary(term_id, client_id):
            # 主客户端变更 → 广播 resize_owner
            primary, locked = tm.get_primary_info(term_id)
            self.broadcast_json({
                "type": "resize_owner",
                "client_id": primary,
                "locked": locked,
            })
            # 用新主客户端的尺寸 resize PTY
            sizes = tm._client_sizes.get(term_id, {})
            if client_id in sizes:
                new_rows, new_cols = sizes[client_id]
                actual = await tm.resize(term_id, new_rows, new_cols)
                if actual is not None:
                    self.broadcast_json({"type": "pty_resize", "rows": actual[0], "cols": actual[1]})

        tm.write(term_id, payload)
