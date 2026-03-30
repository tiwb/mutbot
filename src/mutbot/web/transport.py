"""统一 WebSocket 可靠传输层。

提供 SendBuffer、Client、varint 编解码等基础设施，
支持 channel 多路复用和断线重连时的消息重发。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Literal

import mutobj
from mutobj import impl

from mutbot.channel import Channel

if TYPE_CHECKING:
    from mutagent.net.server import WebSocketConnection as WebSocket

logger = logging.getLogger(__name__)

FrameType = Literal["json", "binary"]


class BufferOverflow(Exception):
    """发送缓冲区溢出。"""


class SendBuffer:
    """已发送但未被对方 ACK 确认的消息缓冲区。

    每条消息以 ``(frame_type, data)`` 形式存储：
    - ``("json", dict)`` — JSON 消息（Text Frame）
    - ``("binary", bytes)`` — 二进制消息（Binary Frame）

    ``_buffer[0]`` 对应对方应收到的第 ``_peer_ack + 1`` 条消息，
    ``_buffer[-1]`` 对应第 ``_total_sent`` 条消息。
    """

    MAX_MESSAGES: int = 1000
    MAX_BYTES: int = 1 * 1024 * 1024  # 1 MB

    def __init__(self) -> None:
        self._buffer: deque[tuple[FrameType, dict | bytes]] = deque()
        self._total_sent: int = 0
        self._peer_ack: int = 0
        self._current_bytes: int = 0

    # -- 属性 ---------------------------------------------------------------

    @property
    def total_sent(self) -> int:
        return self._total_sent

    @property
    def peer_ack(self) -> int:
        return self._peer_ack

    @property
    def pending_count(self) -> int:
        return len(self._buffer)

    # -- 核心操作 -----------------------------------------------------------

    def append(self, frame_type: FrameType, data: dict | bytes) -> None:
        """将消息存入缓冲区，递增 total_sent。

        调用方负责实际写入 WebSocket。溢出时抛出 ``BufferOverflow``，
        消息不会被存入缓冲区。
        """
        size = self._estimate_size(frame_type, data)
        new_count = len(self._buffer) + 1
        new_bytes = self._current_bytes + size
        if new_count > self.MAX_MESSAGES or new_bytes > self.MAX_BYTES:
            raise BufferOverflow(
                f"send buffer overflow: {new_count} msgs, {new_bytes} bytes"
            )
        self._buffer.append((frame_type, data))
        self._total_sent += 1
        self._current_bytes = new_bytes

    def on_ack(self, n: int) -> None:
        """收到对方 ACK(n)：安全丢弃前 n 条已确认消息。"""
        if n < self._peer_ack or n > self._total_sent:
            logger.warning(
                "invalid ACK %d (peer_ack=%d, total_sent=%d)",
                n, self._peer_ack, self._total_sent,
            )
            return
        discard = n - self._peer_ack
        for _ in range(discard):
            frame_type, data = self._buffer.popleft()
            self._current_bytes -= self._estimate_size(frame_type, data)
        self._peer_ack = n

    def replay(self, last_peer_count: int) -> list[tuple[FrameType, dict | bytes]]:
        """重连后：返回对方未收到的消息列表。

        ``last_peer_count`` 是对方报告的实际接收计数。
        返回 buffer 中对方未收到的部分（跳过已收到的）。
        """
        skip = last_peer_count - self._peer_ack
        if skip < 0 or skip > len(self._buffer):
            logger.warning(
                "replay skip out of range: last_peer_count=%d, peer_ack=%d, buffer=%d",
                last_peer_count, self._peer_ack, len(self._buffer),
            )
            return []
        return list(self._buffer)[skip:]

    def can_resume(self, last_peer_count: int) -> bool:
        """检查对方的 last_seq 是否在 buffer 可覆盖范围内。"""
        return self._peer_ack <= last_peer_count <= self._total_sent

    def reset(self) -> None:
        """完整重置（用于 resumed=false 场景）。"""
        self._buffer.clear()
        self._total_sent = 0
        self._peer_ack = 0
        self._current_bytes = 0

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _estimate_size(frame_type: FrameType, data: dict | bytes) -> int:
        if frame_type == "binary":
            return len(data) if isinstance(data, (bytes, bytearray)) else 0
        # JSON: 粗略估算序列化后大小
        try:
            return len(json.dumps(data, ensure_ascii=False).encode())
        except Exception:
            return sys.getsizeof(data)


# ---------------------------------------------------------------------------
# varint (LEB128) 编解码
# ---------------------------------------------------------------------------


def encode_varint(n: int) -> bytes:
    """将非负整数编码为 LEB128 varint。"""
    if n < 0:
        raise ValueError(f"varint must be non-negative, got {n}")
    if n == 0:
        return b"\x00"
    parts: list[int] = []
    while n > 0:
        byte = n & 0x7F
        n >>= 7
        if n > 0:
            byte |= 0x80
        parts.append(byte)
    return bytes(parts)


def decode_varint(data: bytes | bytearray | memoryview, offset: int = 0) -> tuple[int, int]:
    """从 data[offset:] 解码一个 LEB128 varint。

    返回 ``(value, bytes_consumed)``。
    """
    result = 0
    shift = 0
    pos = offset
    while pos < len(data):
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not (byte & 0x80):
            return result, pos - offset
        shift += 7
    raise ValueError("truncated varint")


# ---------------------------------------------------------------------------
# Client — 连接级可靠传输
# ---------------------------------------------------------------------------

ClientState = Literal["connected", "buffering", "expired"]


def _origin_from_headers(headers: dict[str, str]) -> str:
    """从 WebSocket 握手 headers 推算 origin（scheme://host）。"""
    host = headers.get("host", "")
    if not host:
        return ""
    scheme = "https" if headers.get("x-forwarded-proto") == "https" else "http"
    return f"{scheme}://{host}"


class Client:
    """一个 WebSocket 连接的可靠传输层。

    管理 send buffer、接收计数、ACK 定时器、死连接检测和 buffering 超时。
    所有发送通过统一的 ``_send_queue`` 串行化，保证全局消息顺序。
    """

    ACK_INTERVAL: float = 5.0        # 心跳间隔（秒）— 无新内容时的保活信号
    # ACK_BATCH 已废弃：改为即时 ACK（每收到内容消息立即回复）
    DEAD_TIMEOUT: float = 15.0       # 死连接超时（秒）
    BUFFER_TIMEOUT: float = 30.0     # buffering 超时（秒）
    BINARY_PAUSE_THRESHOLD: int = 200  # 终端帧背压阈值（pending 消息数）

    def __init__(
        self,
        client_id: str,
        workspace_id: str,
        ws: WebSocket,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.client_id = client_id
        self.workspace_id = workspace_id
        self.ws: WebSocket | None = ws
        self.state: ClientState = "connected"
        self.origin: str = _origin_from_headers(getattr(ws, "headers", {}))

        # 可靠传输 — 发送 (Server→Client)
        self._send_queue: asyncio.Queue[tuple[FrameType, Any]] = asyncio.Queue()
        self._send_buffer = SendBuffer()

        # 可靠传输 — 接收 (Client→Server)
        self._recv_count: int = 0

        # 定时器
        self._loop: asyncio.AbstractEventLoop | None = loop
        self._ack_handle: asyncio.TimerHandle | None = None
        self._dead_handle: asyncio.TimerHandle | None = None
        self._expire_handle: asyncio.TimerHandle | None = None
        self._send_task: asyncio.Task[None] | None = None

        # 回调
        self._on_expire: list[Any] = []  # callable[Client] → None
        self._on_disconnect: list[Any] = []  # callable[Client] → None

        # ACK 批量触发计数
        self._recv_since_last_ack: int = 0
        self._closed = False

        # 终端帧背压
        self._binary_paused = False
        self._on_binary_resume: list[Any] = []  # callable[Client] → None

        # 诊断：上次收到客户端数据的时间
        self._last_recv_time: float = time.monotonic()

    # -- 属性 ---------------------------------------------------------------

    @property
    def recv_count(self) -> int:
        return self._recv_count

    @property
    def send_buffer(self) -> SendBuffer:
        return self._send_buffer

    def binary_allowed(self) -> bool:
        """终端帧（binary）是否允许推送。

        buffering/expired 状态或背压超限时返回 False，调用方应丢弃帧。
        """
        if self.state != "connected":
            return False
        if self._send_buffer.pending_count >= self.BINARY_PAUSE_THRESHOLD:
            if not self._binary_paused:
                self._binary_paused = True
                logger.info("Client %s: binary paused (pending=%d)",
                            self.client_id, self._send_buffer.pending_count)
            return False
        return True

    def on_binary_resume(self, callback: Any) -> None:
        """注册背压恢复回调（用于触发 snapshot 刷新）。"""
        self._on_binary_resume.append(callback)

    # -- 生命周期 -----------------------------------------------------------

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def start(self) -> None:
        """启动 send worker 和定时器。连接建立后调用。"""
        loop = self._get_loop()
        self._send_task = loop.create_task(self._send_worker())
        self._schedule_ack()
        self._reset_dead_timer()

    def stop(self) -> None:
        """停止所有任务和定时器。"""
        self._closed = True
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
        self._cancel_timers()

    # -- 发送 ---------------------------------------------------------------

    def enqueue(self, frame_type: FrameType, data: Any) -> None:
        """线程安全入队，通过 call_soon_threadsafe 唤醒事件循环。"""
        loop = self._get_loop()
        loop.call_soon_threadsafe(self._send_queue.put_nowait, (frame_type, data))

    # -- 接收 ---------------------------------------------------------------

    def on_content_received(self) -> None:
        """收到一条内容消息时调用。递增接收计数，重置死连接计时器，即时回复 ACK。"""
        self._recv_count += 1
        self._recv_since_last_ack += 1
        self._reset_dead_timer()
        # 即时 ACK：每收到内容消息立即回复
        self._send_ack_now()

    def on_control_received(self) -> None:
        """收到控制消息（ack/welcome）时调用。仅重置死连接计时器。"""
        self._reset_dead_timer()

    def on_peer_ack(self, n: int) -> None:
        """收到对方的 ACK(n)。"""
        self._send_buffer.on_ack(n)
        self._reset_dead_timer()
        # 背压恢复（滞后阈值 = 1/2，避免频繁切换）
        if self._binary_paused and self._send_buffer.pending_count < self.BINARY_PAUSE_THRESHOLD // 2:
            self._binary_paused = False
            logger.info("Client %s: binary resumed (pending=%d)",
                        self.client_id, self._send_buffer.pending_count)
            for cb in self._on_binary_resume:
                try:
                    cb(self)
                except Exception:
                    logger.exception("binary resume callback error")

    # -- 断线 / 重连 --------------------------------------------------------

    def enter_buffering(self) -> None:
        """WebSocket 断开，进入 buffering 状态。"""
        if self.state == "expired":
            return
        self.state = "buffering"
        ws = self.ws
        self.ws = None
        if ws is not None:
            # 主动关闭 WebSocket，让前端收到 onclose 触发重连
            asyncio.ensure_future(self._close_ws(ws))
        self._cancel_timers()
        # 触发断连回调
        for cb in self._on_disconnect:
            try:
                cb(self)
            except Exception:
                logger.exception("disconnect callback error")
        # 启动 buffering 超时
        self._expire_handle = self._get_loop().call_later(
            self.BUFFER_TIMEOUT, self._expire,
        )
        logger.info("Client %s entering buffering", self.client_id)

    def resume(self, ws: WebSocket, last_seq: int) -> bool:
        """尝试恢复连接。返回是否恢复成功。"""
        if self.state == "expired":
            return False
        if not self._send_buffer.can_resume(last_seq):
            return False

        # 恢复成功
        self.ws = ws
        self.state = "connected"
        if self._expire_handle:
            self._expire_handle.cancel()
            self._expire_handle = None

        self._schedule_ack()
        self._reset_dead_timer()
        return True

    def get_replay_messages(self, last_seq: int) -> list[tuple[FrameType, dict | bytes]]:
        """获取需要重发给 client 的消息。"""
        return self._send_buffer.replay(last_seq)

    def reset_for_fresh_connection(self, ws: WebSocket) -> None:
        """完全重置（resumed=false 场景）。"""
        self.ws = ws
        self.state = "connected"
        self._send_buffer.reset()
        self._recv_count = 0
        self._recv_since_last_ack = 0
        # 清空 send queue
        while not self._send_queue.empty():
            try:
                self._send_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._cancel_timers()
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
        self._closed = False
        self._send_task = self._get_loop().create_task(self._send_worker())
        self._schedule_ack()
        self._reset_dead_timer()

    # -- 过期回调 -----------------------------------------------------------

    def on_expire(self, callback: Any) -> None:
        """注册过期回调。"""
        self._on_expire.append(callback)

    def on_disconnect(self, callback: Any) -> None:
        """注册断连回调（WebSocket 断开时触发，早于过期）。"""
        self._on_disconnect.append(callback)

    # -- 内部：send worker --------------------------------------------------

    async def _send_worker(self) -> None:
        """从 send queue 取消息，存入 send buffer，写入 WebSocket。"""
        while not self._closed:
            try:
                frame_type, data = await self._send_queue.get()
            except asyncio.CancelledError:
                return
            try:
                self._send_buffer.append(frame_type, data)
            except BufferOverflow:
                logger.warning("Client %s send buffer overflow", self.client_id)
                self._expire()
                return
            if self.ws is not None:
                try:
                    await self._ws_send(frame_type, data)
                except Exception:
                    logger.debug("Client %s ws send failed", self.client_id, exc_info=True)

    async def _ws_send(self, frame_type: FrameType, data: Any) -> None:
        """实际写入 WebSocket。"""
        if self.ws is None:
            return
        if frame_type == "json":
            await self.ws.send_json(data)
        else:
            # binary: data 是 bytes
            await self.ws.send_bytes(data)

    # -- 内部：定时器 -------------------------------------------------------

    def _schedule_ack(self) -> None:
        """安排下一次 ACK 发送。"""
        if self._ack_handle:
            self._ack_handle.cancel()
        self._ack_handle = self._get_loop().call_later(
            self.ACK_INTERVAL, self._send_ack_now,
        )

    def _send_ack_now(self) -> None:
        """立即发送 ACK（作为心跳 + 确认）。"""
        self._recv_since_last_ack = 0
        ack_msg: dict[str, Any] = {"type": "ack", "ack": self._recv_count}
        # ACK 是控制消息，不进入 send buffer，直接写 ws
        if self.ws is not None:
            asyncio.ensure_future(self._ws_send_control(ack_msg))
        self._schedule_ack()

    async def _ws_send_control(self, data: dict) -> None:
        """发送控制消息（不经过 send buffer）。"""
        if self.ws is None:
            return
        try:
            await self.ws.send_json(data)
        except Exception:
            logger.debug("Client %s control send failed", self.client_id, exc_info=True)

    @staticmethod
    async def _close_ws(ws: WebSocket) -> None:
        """安全关闭 WebSocket。"""
        try:
            await ws.close()
        except Exception:
            pass

    def _reset_dead_timer(self) -> None:
        """重置死连接检测计时器。"""
        self._last_recv_time = time.monotonic()
        if self._dead_handle:
            self._dead_handle.cancel()
        if self.state == "connected":
            self._dead_handle = self._get_loop().call_later(
                self.DEAD_TIMEOUT, self._on_dead_timeout,
            )

    def _on_dead_timeout(self) -> None:
        """死连接超时：进入 buffering。"""
        silence = time.monotonic() - self._last_recv_time
        logger.warning("Client %s dead timeout (no data for %.1fs)",
                       self.client_id, silence)
        self.enter_buffering()

    def _expire(self) -> None:
        """Client 过期：清理所有资源。"""
        if self.state == "expired":
            return
        logger.info("Client %s expired", self.client_id)
        self.state = "expired"
        self._cancel_timers()
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
        for cb in self._on_expire:
            try:
                cb(self)
            except Exception:
                logger.exception("expire callback error")

    def _cancel_timers(self) -> None:
        for handle in (self._ack_handle, self._dead_handle, self._expire_handle):
            if handle:
                handle.cancel()
        self._ack_handle = None
        self._dead_handle = None
        self._expire_handle = None


# ---------------------------------------------------------------------------
# ChannelTransport — Channel 的 WebSocket 传输实现
# ---------------------------------------------------------------------------


class ChannelTransport(mutobj.Extension[Channel]):
    """Channel 的 WebSocket 传输状态——对 Session 透明。"""

    _client: Client | None = None


@impl(Channel.send_json)
def _channel_send_json(self: Channel, data: dict) -> None:
    ext = ChannelTransport.get(self)
    if ext and ext._client:
        ext._client.enqueue("json", {"ch": self.ch, **data})


@impl(Channel.send_binary)
def _channel_send_binary(self: Channel, data: bytes) -> None:
    ext = ChannelTransport.get(self)
    if ext and ext._client:
        # 终端帧流控：buffering/expired 或背压超限时丢弃
        if not ext._client.binary_allowed():
            return
        prefix = encode_varint(self.ch)
        ext._client.enqueue("binary", prefix + data)


# ---------------------------------------------------------------------------
# ChannelManager — 全局频道管理
# ---------------------------------------------------------------------------


class ChannelManager:
    """管理所有 Channel 的分配、路由和生命周期。

    线程安全：内部使用 ``threading.Lock`` 保护映射表。
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._channels: dict[int, Channel] = {}
        self._session_channels: dict[str, set[int]] = {}
        self._client_channels: dict[str, set[int]] = {}
        self._next_id = 1
        self._free_ids: list[int] = []  # 回收的 ID（最小堆）

    def open(
        self,
        client: Client,
        session_id: str | None = None,
    ) -> Channel:
        """打开频道，分配全局唯一 channel ID（复用最小可用自然数）。"""
        import heapq
        with self._lock:
            if self._free_ids:
                ch_id = heapq.heappop(self._free_ids)
            else:
                ch_id = self._next_id
                self._next_id += 1

            channel = Channel(ch=ch_id, session_id=session_id or "")
            ChannelTransport.get_or_create(channel)._client = client
            self._channels[ch_id] = channel
            if session_id:
                self._session_channels.setdefault(session_id, set()).add(ch_id)
            self._client_channels.setdefault(client.client_id, set()).add(ch_id)
            return channel

    def close(self, ch: int) -> Channel | None:
        """关闭频道，回收 channel ID。返回被关闭的 Channel，不存在则返回 None。"""
        import heapq
        with self._lock:
            channel = self._channels.pop(ch, None)
            if channel is None:
                return None
            heapq.heappush(self._free_ids, ch)
            if channel.session_id:
                s = self._session_channels.get(channel.session_id)
                if s:
                    s.discard(ch)
                    if not s:
                        del self._session_channels[channel.session_id]
            ext = ChannelTransport.get(channel)
            client_id = ext._client.client_id if ext and ext._client else None
            if client_id:
                c = self._client_channels.get(client_id)
                if c:
                    c.discard(ch)
                    if not c:
                        del self._client_channels[client_id]
            return channel

    def get_channel(self, ch: int) -> Channel | None:
        """按 ch 查找 channel。"""
        with self._lock:
            return self._channels.get(ch)

    def get_channels_for_session(self, session_id: str) -> list[Channel]:
        """线程安全快照：获取连接到指定 session 的所有 channel。"""
        with self._lock:
            ch_ids = self._session_channels.get(session_id)
            if not ch_ids:
                return []
            return [self._channels[ch] for ch in ch_ids if ch in self._channels]

    def get_channels_for_client(self, client_id: str) -> list[Channel]:
        """线程安全快照：获取指定 client 的所有 channel。"""
        with self._lock:
            ch_ids = self._client_channels.get(client_id)
            if not ch_ids:
                return []
            return [self._channels[ch] for ch in ch_ids if ch in self._channels]

    def close_all_for_client(self, client: Client) -> list[Channel]:
        """关闭指定 client 的所有频道，回收 ID。返回被关闭的 Channel 列表。"""
        import heapq
        with self._lock:
            ch_ids = self._client_channels.pop(client.client_id, None)
            if not ch_ids:
                return []
            closed: list[Channel] = []
            for ch in list(ch_ids):
                channel = self._channels.pop(ch, None)
                if channel is None:
                    continue
                heapq.heappush(self._free_ids, ch)
                if channel.session_id:
                    s = self._session_channels.get(channel.session_id)
                    if s:
                        s.discard(ch)
                        if not s:
                            del self._session_channels[channel.session_id]
                closed.append(channel)
            return closed
