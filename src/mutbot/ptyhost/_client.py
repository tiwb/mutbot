"""ptyhost WebSocket 客户端 — mutbot 侧连接 ptyhost 守护进程。

基于 wsproto + asyncio raw socket，零新依赖。
支持 async 命令（await 回复）和 fire-and-forget 命令（不等回复）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Callable

import wsproto
import wsproto.events as ws_events

logger = logging.getLogger("mutbot.ptyhost.client")

# 回调类型
OutputCallback = Callable[[str, bytes], None]   # (term_id, data)
ExitCallback = Callable[[str, int | None], None]  # (term_id, exit_code)


class PtyHostClient:
    """ptyhost WebSocket 客户端。

    用法::

        client = PtyHostClient("127.0.0.1", port)
        client.on_output = lambda term_id, data: ...
        client.on_exit = lambda term_id, exit_code: ...
        await client.connect()

        term_id = await client.create(24, 80)
        client.write(term_id, b"ls\\n")
        scrollback = await client.get_scrollback(term_id)
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
        self.on_output: OutputCallback | None = None
        self.on_exit: ExitCallback | None = None

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
                data = await self._reader.read(4096)  # type: ignore[union-attr]
                if not data:
                    break
                self._ws.receive_data(data)  # type: ignore[union-attr]
                for event in self._ws.events():  # type: ignore[union-attr]
                    self._process_event(event)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("ptyhost receive loop ended", exc_info=True)
        finally:
            self._connected = False

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
        """处理 JSON 消息：区分事件和命令回复。"""
        if "type" in msg:
            # 事件（exit）
            if msg["type"] == "exit" and self.on_exit:
                self.on_exit(msg["term_id"], msg.get("exit_code"))
        else:
            # 命令回复：按 seq 匹配到 pending future
            seq = msg.pop("seq", None)
            if seq is not None:
                future = self._pending.pop(seq, None)
                if future and not future.done():
                    future.set_result(msg)
            # 无 seq 或无匹配 future → 静默丢弃（fire-and-forget 的回复）

    def _on_binary(self, data: bytes) -> None:
        """处理 binary 帧：[16 bytes UUID][PTY 输出数据]。"""
        if len(data) < 16:
            return
        term_id = data[:16].hex()
        payload = data[16:]
        if self.on_output:
            self.on_output(term_id, payload)

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

    async def get_scrollback(self, term_id: str) -> bytes:
        """获取终端 scrollback 数据。"""
        reply = await self._send_command({"cmd": "scrollback", "term_id": term_id})
        data_b64 = reply.get("data_b64", "")
        return base64.b64decode(data_b64) if data_b64 else b""

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
