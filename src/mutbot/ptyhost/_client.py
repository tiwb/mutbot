"""ptyhost WebSocket 客户端 — mutbot 侧连接 ptyhost 守护进程。

基于 wsproto + asyncio raw socket，零新依赖。
支持 async 命令（await 回复）和 fire-and-forget 命令（不等回复）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import wsproto
import wsproto.events as ws_events

logger = logging.getLogger("mutbot.ptyhost.client")

# 回调类型
FrameCallback = Callable[[str, str, bytes], None]   # (term_id, view_id, ansi_frame)
ExitCallback = Callable[[str, int | None], None]  # (term_id, exit_code)
DisconnectCallback = Callable[[], None]  # ptyhost 连接断开


class PtyHostClient:
    """ptyhost WebSocket 客户端。

    用法::

        client = PtyHostClient("127.0.0.1", port)
        client.on_frame = lambda term_id, view_id, data: ...
        client.on_exit = lambda term_id, exit_code: ...
        await client.connect()

        term_id = await client.create(24, 80)
        view_id = await client.create_view(term_id)
        client.write(term_id, b"ls\\n")
        snapshot = await client.get_snapshot(view_id)
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._ws: wsproto.WSConnection | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._connected = False

        # seq 匹配：每个命令带递增 seq，回复中回传 seq
        self._next_seq = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}

        # 消息累积缓冲（处理 WebSocket 分片）
        self._text_buffer: list[str] = []
        self._bytes_buffer: list[bytes] = []

        # 回调
        self.on_frame: FrameCallback | None = None
        self.on_exit: ExitCallback | None = None
        self.on_disconnect: DisconnectCallback | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """连接 ptyhost 并完成 WebSocket 握手。"""
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port,
        )
        self._ws = wsproto.WSConnection(wsproto.ConnectionType.CLIENT)

        # 发送 HTTP upgrade 请求
        request = ws_events.Request(
            host=f"{self._host}:{self._port}", target="/",
        )
        self._writer.write(self._ws.send(request))
        await self._writer.drain()

        # 等待 accept
        data = await self._reader.read(4096)
        if not data:
            raise ConnectionError("ptyhost connection closed during handshake")
        self._ws.receive_data(data)
        accepted = False
        for event in self._ws.events():
            if isinstance(event, ws_events.AcceptConnection):
                accepted = True
                break
            elif isinstance(event, ws_events.RejectConnection):
                raise ConnectionError("ptyhost rejected WebSocket connection")
        if not accepted:
            raise ConnectionError("ptyhost handshake incomplete")

        self._connected = True
        self._recv_task = asyncio.create_task(self._receive_loop())
        logger.info("Connected to ptyhost at %s:%d", self._host, self._port)

    async def close(self) -> None:
        """关闭连接。"""
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws and self._writer and not self._writer.is_closing():
            try:
                self._writer.write(
                    self._ws.send(ws_events.CloseConnection(code=1000, reason="bye")),
                )
                await self._writer.drain()
            except Exception:
                pass
        if self._writer:
            self._writer.close()
        self._connected = False
        # 清理 pending futures
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    # ------------------------------------------------------------------
    # 后台接收循环
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """后台任务：接收 ptyhost 推送的输出和事件。"""
        try:
            while True:
                data = await self._reader.read(65536)  # type: ignore[union-attr]
                if not data:
                    break
                self._ws.receive_data(data)  # type: ignore[union-attr]
                for event in self._ws.events():  # type: ignore[union-attr]
                    self._process_event(event)
        except asyncio.CancelledError:
            return  # 正常 cancel（close() 调用），不触发 on_disconnect
        except Exception:
            logger.warning("ptyhost receive loop ended unexpectedly", exc_info=True)
        finally:
            self._connected = False
            # 取消所有 pending futures
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("ptyhost disconnected"))
            self._pending.clear()
        # 通知上层 ptyhost 断开（仅非 cancel 路径）
        if self.on_disconnect:
            try:
                self.on_disconnect()
            except Exception:
                logger.warning("on_disconnect callback error", exc_info=True)

    def _process_event(self, event: Any) -> None:
        if isinstance(event, ws_events.TextMessage):
            self._text_buffer.append(event.data)
            if event.message_finished:
                text = "".join(self._text_buffer)
                self._text_buffer.clear()
                self._on_text(json.loads(text))
        elif isinstance(event, ws_events.BytesMessage):
            self._bytes_buffer.append(event.data)
            if event.message_finished:
                raw = b"".join(self._bytes_buffer)
                self._bytes_buffer.clear()
                self._on_binary(raw)
        elif isinstance(event, ws_events.CloseConnection):
            self._connected = False

    def _on_text(self, msg: dict[str, Any]) -> None:
        """处理 JSON 消息：区分事件、日志转发和命令回复。"""
        if "type" in msg:
            msg_type = msg["type"]
            if msg_type == "exit" and self.on_exit:
                self.on_exit(msg["term_id"], msg.get("exit_code"))
            elif msg_type == "log":
                # ptyhost 日志转发 → 注入本地 logging
                log_logger = logging.getLogger(msg.get("logger", "mutbot.ptyhost"))
                level = getattr(logging, msg.get("level", "INFO"), logging.INFO)
                log_logger.log(level, "[ptyhost] %s", msg.get("message", ""))
        else:
            # 命令回复：按 seq 匹配到 pending future
            seq = msg.pop("seq", None)
            if seq is not None:
                future = self._pending.pop(seq, None)
                if future and not future.done():
                    future.set_result(msg)
            # 无 seq 或无匹配 future → 静默丢弃（fire-and-forget 的回复）

    def _on_binary(self, data: bytes) -> None:
        """处理 binary 帧：[term_id 16B][view_id 8B][ANSI frame]。"""
        if len(data) < 24:
            return
        term_id = data[:16].hex()
        view_id = data[16:24].rstrip(b"\0").decode("ascii")
        frame = data[24:]
        if self.on_frame:
            self.on_frame(term_id, view_id, frame)

    # ------------------------------------------------------------------
    # 命令发送
    # ------------------------------------------------------------------

    def _alloc_seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    async def _send_command(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """发送 JSON 命令并等待回复（通过 seq 匹配）。"""
        if not self._connected or not self._ws or not self._writer:
            raise ConnectionError("not connected to ptyhost")
        seq = self._alloc_seq()
        cmd["seq"] = seq
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[seq] = future
        self._writer.write(self._ws.send(ws_events.TextMessage(data=json.dumps(cmd))))
        await self._writer.drain()
        return await future

    def _send_nowait(self, cmd: dict[str, Any]) -> None:
        """发送 JSON 命令，不等回复（fire-and-forget）。

        回复到达时因无匹配 future 会被静默丢弃。
        """
        if not self._connected or not self._ws or not self._writer:
            return
        seq = self._alloc_seq()
        cmd["seq"] = seq
        self._writer.write(self._ws.send(ws_events.TextMessage(data=json.dumps(cmd))))
        # 不 drain — fire-and-forget

    # ------------------------------------------------------------------
    # Async 命令（等待回复）
    # ------------------------------------------------------------------

    async def create(
        self, rows: int, cols: int, cwd: str | None = None,
    ) -> str:
        """创建终端，返回 term_id (UUID hex)。"""
        cmd: dict[str, Any] = {"cmd": "create", "rows": rows, "cols": cols}
        if cwd:
            cmd["cwd"] = cwd
        reply = await self._send_command(cmd)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "create failed"))
        return reply["term_id"]

    async def create_view(
        self, term_id: str, viewport_rows: int = 0, viewport_cols: int = 0,
    ) -> str:
        """创建 view，返回 view_id。"""
        reply = await self._send_command({
            "cmd": "create_view", "term_id": term_id,
            "viewport_rows": viewport_rows, "viewport_cols": viewport_cols,
        })
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "create_view failed"))
        return reply["view_id"]

    async def destroy_view(self, view_id: str) -> None:
        """销毁 view。"""
        await self._send_command({"cmd": "destroy_view", "view_id": view_id})

    async def set_viewport(self, view_id: str, rows: int, cols: int = 0) -> None:
        """设置 view 的 viewport 尺寸。"""
        await self._send_command({
            "cmd": "set_viewport", "view_id": view_id, "rows": rows, "cols": cols,
        })

    async def get_snapshot(self, view_id: str) -> None:
        """请求 view 快照（帧通过 on_frame 回调异步到达）。"""
        await self._send_command({"cmd": "snapshot", "view_id": view_id})

    async def scroll(self, view_id: str, lines: int) -> None:
        """滚动 view。lines>0 向上，lines<0 向下。"""
        await self._send_command({"cmd": "scroll", "view_id": view_id, "lines": lines})

    async def scroll_to(self, view_id: str, offset: int) -> None:
        """滚动 view 到绝对偏移。offset=0 为 live，>0 为从底部往上行数。"""
        await self._send_command({"cmd": "scroll_to", "view_id": view_id, "offset": offset})

    async def scroll_to_bottom(self, view_id: str) -> None:
        """view 回到 live。"""
        await self._send_command({"cmd": "scroll_to_bottom", "view_id": view_id})

    async def clear_scrollback(self, term_id: str) -> None:
        """清除终端的 scrollback 历史缓冲。"""
        await self._send_command({"cmd": "clear_scrollback", "term_id": term_id})

    async def get_scroll_state(self, view_id: str) -> dict[str, int]:
        """获取滚动状态。"""
        return await self._send_command({"cmd": "scroll_state", "view_id": view_id})

    async def resize(
        self, term_id: str, rows: int, cols: int,
    ) -> tuple[int, int] | None:
        """调整终端大小（async）。"""
        reply = await self._send_command({
            "cmd": "resize", "term_id": term_id, "rows": rows, "cols": cols,
        })
        if reply.get("ok"):
            return (reply["rows"], reply["cols"])
        return None

    async def list_terminals(self) -> list[dict[str, Any]]:
        """列出所有终端。"""
        reply = await self._send_command({"cmd": "list"})
        return reply.get("terminals", [])

    async def kill(self, term_id: str) -> None:
        """终止终端（async）。"""
        await self._send_command({"cmd": "kill", "term_id": term_id})

    async def shutdown(self) -> dict[str, Any]:
        """请求 ptyhost 关闭。"""
        return await self._send_command({"cmd": "shutdown"})

    async def status(self, term_id: str) -> dict[str, Any]:
        """查询终端状态。"""
        return await self._send_command({"cmd": "status", "term_id": term_id})

    # ------------------------------------------------------------------
    # Fire-and-forget 命令
    # ------------------------------------------------------------------

    def kill_nowait(self, term_id: str) -> None:
        """终止终端（fire-and-forget）。"""
        self._send_nowait({"cmd": "kill", "term_id": term_id})

    def resize_nowait(self, term_id: str, rows: int, cols: int) -> None:
        """调整终端大小（fire-and-forget）。"""
        self._send_nowait({
            "cmd": "resize", "term_id": term_id, "rows": rows, "cols": cols,
        })

    # ------------------------------------------------------------------
    # Binary I/O（fire-and-forget）
    # ------------------------------------------------------------------

    def write(self, term_id: str, data: bytes) -> None:
        """发送键盘输入到终端（fire-and-forget）。"""
        if not self._connected or not self._ws or not self._writer:
            return
        frame = bytes.fromhex(term_id) + data
        self._writer.write(self._ws.send(ws_events.BytesMessage(data=frame)))
