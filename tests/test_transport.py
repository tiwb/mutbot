"""测试可靠传输层 — SendBuffer + varint 编解码 + Client。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mutbot.web.transport import (
    BufferOverflow,
    Channel,
    ChannelManager,
    ChannelTransport,
    Client,
    SendBuffer,
    decode_varint,
    encode_varint,
)


# ===================================================================
# SendBuffer
# ===================================================================


class TestSendBuffer:
    """SendBuffer 核心功能。"""

    def test_append_and_total_sent(self) -> None:
        buf = SendBuffer()
        assert buf.total_sent == 0
        assert buf.pending_count == 0

        buf.append("json", {"type": "hello"})
        assert buf.total_sent == 1
        assert buf.pending_count == 1

        buf.append("binary", b"\x01data")
        assert buf.total_sent == 2
        assert buf.pending_count == 2

    def test_on_ack_discards_confirmed(self) -> None:
        buf = SendBuffer()
        buf.append("json", {"a": 1})
        buf.append("json", {"a": 2})
        buf.append("json", {"a": 3})
        assert buf.pending_count == 3

        buf.on_ack(2)
        assert buf.peer_ack == 2
        assert buf.pending_count == 1
        # 剩余的应该是第 3 条
        remaining = buf.replay(2)
        assert len(remaining) == 1
        assert remaining[0] == ("json", {"a": 3})

    def test_on_ack_all(self) -> None:
        buf = SendBuffer()
        buf.append("json", {"x": 1})
        buf.append("json", {"x": 2})
        buf.on_ack(2)
        assert buf.pending_count == 0
        assert buf.peer_ack == 2

    def test_on_ack_invalid_too_small(self) -> None:
        buf = SendBuffer()
        buf.append("json", {"x": 1})
        buf.on_ack(1)
        # ACK 小于 peer_ack → 忽略
        buf.on_ack(0)
        assert buf.peer_ack == 1

    def test_on_ack_invalid_too_large(self) -> None:
        buf = SendBuffer()
        buf.append("json", {"x": 1})
        # ACK 大于 total_sent → 忽略
        buf.on_ack(5)
        assert buf.peer_ack == 0
        assert buf.pending_count == 1

    def test_replay_from_peer_count(self) -> None:
        buf = SendBuffer()
        buf.append("json", {"n": 1})
        buf.append("binary", b"\x01")
        buf.append("json", {"n": 3})
        buf.append("json", {"n": 4})

        # 对方收到了 2 条，需要重发第 3、4 条
        msgs = buf.replay(2)
        assert len(msgs) == 2
        assert msgs[0] == ("json", {"n": 3})
        assert msgs[1] == ("json", {"n": 4})

    def test_replay_after_partial_ack(self) -> None:
        buf = SendBuffer()
        for i in range(5):
            buf.append("json", {"n": i})

        # 先 ACK 了 2 条
        buf.on_ack(2)
        assert buf.pending_count == 3  # buffer 里还有 3 条 (n=2,3,4)

        # 对方实际收到了 3 条（buffer 中跳过 1 条）
        msgs = buf.replay(3)
        assert len(msgs) == 2
        assert msgs[0] == ("json", {"n": 3})
        assert msgs[1] == ("json", {"n": 4})

    def test_replay_nothing_to_resend(self) -> None:
        buf = SendBuffer()
        buf.append("json", {"x": 1})
        buf.append("json", {"x": 2})
        # 对方已收到全部
        msgs = buf.replay(2)
        assert msgs == []

    def test_replay_out_of_range(self) -> None:
        buf = SendBuffer()
        buf.append("json", {"x": 1})
        # last_peer_count 超出 buffer → 返回空
        assert buf.replay(5) == []
        # last_peer_count 小于 peer_ack → 返回空
        buf.on_ack(1)
        assert buf.replay(0) == []

    def test_can_resume(self) -> None:
        buf = SendBuffer()
        buf.append("json", {"x": 1})
        buf.append("json", {"x": 2})
        buf.append("json", {"x": 3})
        buf.on_ack(1)

        assert buf.can_resume(1) is True  # peer_ack
        assert buf.can_resume(2) is True  # 中间
        assert buf.can_resume(3) is True  # total_sent
        assert buf.can_resume(0) is False  # 低于 peer_ack
        assert buf.can_resume(4) is False  # 超过 total_sent

    def test_reset(self) -> None:
        buf = SendBuffer()
        buf.append("json", {"x": 1})
        buf.append("json", {"x": 2})
        buf.on_ack(1)
        buf.reset()
        assert buf.total_sent == 0
        assert buf.peer_ack == 0
        assert buf.pending_count == 0

    def test_mixed_json_binary_counting(self) -> None:
        """JSON 和 Binary 共享同一个计数器。"""
        buf = SendBuffer()
        buf.append("json", {"type": "hello"})
        buf.append("binary", b"\x01output")
        buf.append("json", {"type": "delta"})
        buf.append("binary", b"\x02\x00\x18\x00\x50")
        assert buf.total_sent == 4
        assert buf.pending_count == 4

        buf.on_ack(3)
        assert buf.pending_count == 1
        remaining = buf.replay(3)
        assert len(remaining) == 1
        assert remaining[0] == ("binary", b"\x02\x00\x18\x00\x50")

    def test_overflow_by_message_count(self) -> None:
        buf = SendBuffer()
        buf.MAX_MESSAGES = 5
        for i in range(5):
            buf.append("json", {"n": i})
        with pytest.raises(BufferOverflow):
            buf.append("json", {"n": 5})

    def test_overflow_by_bytes(self) -> None:
        buf = SendBuffer()
        buf.MAX_BYTES = 100
        # 每条大约 50+ bytes
        buf.append("json", {"data": "x" * 50})
        with pytest.raises(BufferOverflow):
            buf.append("json", {"data": "y" * 50})

    def test_ack_frees_space_for_more(self) -> None:
        """ACK 后 buffer 腾出空间，可继续发送。"""
        buf = SendBuffer()
        buf.MAX_MESSAGES = 3
        buf.append("json", {"n": 1})
        buf.append("json", {"n": 2})
        buf.append("json", {"n": 3})
        # 满了，再发会溢出
        with pytest.raises(BufferOverflow):
            buf.append("json", {"n": 4})
        # ACK 释放空间
        buf.on_ack(2)
        buf.append("json", {"n": 4})  # 不再溢出
        assert buf.pending_count == 2


# ===================================================================
# varint 编解码
# ===================================================================


class TestVarint:
    """LEB128 varint 编解码。"""

    def test_encode_zero(self) -> None:
        assert encode_varint(0) == b"\x00"

    def test_encode_single_byte(self) -> None:
        assert encode_varint(1) == b"\x01"
        assert encode_varint(127) == b"\x7f"

    def test_encode_two_bytes(self) -> None:
        # 128 = 0x80 0x01
        assert encode_varint(128) == bytes([0x80, 0x01])
        # 300 = 0xAC 0x02
        assert encode_varint(300) == bytes([0xAC, 0x02])
        assert encode_varint(16383) == bytes([0xFF, 0x7F])

    def test_encode_three_bytes(self) -> None:
        assert encode_varint(16384) == bytes([0x80, 0x80, 0x01])

    def test_encode_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            encode_varint(-1)

    def test_decode_single_byte(self) -> None:
        assert decode_varint(b"\x01") == (1, 1)
        assert decode_varint(b"\x7f") == (127, 1)

    def test_decode_two_bytes(self) -> None:
        assert decode_varint(bytes([0x80, 0x01])) == (128, 2)
        assert decode_varint(bytes([0xAC, 0x02])) == (300, 2)

    def test_decode_with_offset(self) -> None:
        data = b"\xff\xff" + bytes([0xAC, 0x02]) + b"\x00"
        val, consumed = decode_varint(data, offset=2)
        assert val == 300
        assert consumed == 2

    def test_decode_truncated_raises(self) -> None:
        # 高位续传位置 1 但没有后续字节
        with pytest.raises(ValueError, match="truncated"):
            decode_varint(bytes([0x80]))

    def test_roundtrip(self) -> None:
        """编码后解码应得到原值。"""
        for n in [0, 1, 127, 128, 255, 300, 16383, 16384, 100000]:
            encoded = encode_varint(n)
            val, consumed = decode_varint(encoded)
            assert val == n
            assert consumed == len(encoded)

    def test_decode_zero(self) -> None:
        assert decode_varint(b"\x00") == (0, 1)

    def test_decode_memoryview(self) -> None:
        data = bytearray(bytes([0xAC, 0x02]))
        val, consumed = decode_varint(memoryview(data))
        assert val == 300
        assert consumed == 2


# ===================================================================
# Client
# ===================================================================


def _make_mock_ws() -> MagicMock:
    """创建模拟 WebSocket。"""
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.send_bytes = AsyncMock()
    return ws


class TestClientStateTransitions:
    """Client 状态机转换。"""

    @pytest.mark.asyncio
    async def test_initial_state_connected(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        assert client.state == "connected"

    @pytest.mark.asyncio
    async def test_enter_buffering(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()
        client.enter_buffering()
        assert client.state == "buffering"
        assert client.ws is None
        client.stop()

    @pytest.mark.asyncio
    async def test_buffering_timeout_to_expired(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.BUFFER_TIMEOUT = 0.05  # 50ms for fast test
        client.start()
        client.enter_buffering()
        await asyncio.sleep(0.1)
        assert client.state == "expired"
        client.stop()

    @pytest.mark.asyncio
    async def test_resume_from_buffering(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        # 发送一条消息让 send_buffer 有内容
        client.enqueue("json", {"type": "test"})
        await asyncio.sleep(0.05)

        client.enter_buffering()
        assert client.state == "buffering"

        ws2 = _make_mock_ws()
        ok = client.resume(ws2, last_seq=0)
        assert ok is True
        assert client.state == "connected"
        assert client.ws is ws2
        client.stop()

    @pytest.mark.asyncio
    async def test_resume_fails_when_expired(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.BUFFER_TIMEOUT = 0.01
        client.start()
        client.enter_buffering()
        await asyncio.sleep(0.05)
        assert client.state == "expired"

        ws2 = _make_mock_ws()
        ok = client.resume(ws2, last_seq=0)
        assert ok is False
        client.stop()

    @pytest.mark.asyncio
    async def test_resume_fails_when_last_seq_out_of_range(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()
        client.enter_buffering()

        ws2 = _make_mock_ws()
        # last_seq=5 但 total_sent=0 → 不可覆盖
        ok = client.resume(ws2, last_seq=5)
        assert ok is False
        client.stop()


class TestClientSendWorker:
    """Client send worker 行为。"""

    @pytest.mark.asyncio
    async def test_send_json_writes_to_ws(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        client.enqueue("json",{"type": "hello"})
        await asyncio.sleep(0.05)

        ws.send_json.assert_called_with({"type": "hello"})
        assert client.send_buffer.total_sent == 1
        client.stop()

    @pytest.mark.asyncio
    async def test_send_preserves_order(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        sent_order: list[dict] = []
        original_send_json = ws.send_json

        async def capture_send(data: dict) -> None:
            sent_order.append(data)
            await original_send_json(data)

        ws.send_json = capture_send

        for i in range(5):
            client.enqueue("json",{"n": i})

        await asyncio.sleep(0.1)
        assert [m["n"] for m in sent_order] == [0, 1, 2, 3, 4]
        client.stop()

    @pytest.mark.asyncio
    async def test_send_during_buffering_stored_not_sent(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        client.enter_buffering()
        client.enqueue("json",{"type": "queued"})
        await asyncio.sleep(0.05)

        # 消息在 send_buffer 中但不会发送到 ws（ws 已为 None）
        assert client.send_buffer.total_sent == 1
        client.stop()

    @pytest.mark.asyncio
    async def test_buffer_overflow_triggers_expire(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client._send_buffer.MAX_MESSAGES = 3
        client.start()

        expired = []
        client.on_expire(lambda c: expired.append(c))

        for i in range(5):
            client.enqueue("json",{"n": i})
        await asyncio.sleep(0.1)

        assert client.state == "expired"
        assert len(expired) == 1
        client.stop()


class TestClientRecvCount:
    """Client 接收计数。"""

    @pytest.mark.asyncio
    async def test_content_increments_recv_count(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        assert client.recv_count == 0
        client.on_content_received()
        assert client.recv_count == 1
        client.on_content_received()
        assert client.recv_count == 2
        client.stop()

    @pytest.mark.asyncio
    async def test_control_does_not_increment(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        client.on_control_received()
        assert client.recv_count == 0
        client.stop()


class TestClientAck:
    """Client ACK 和心跳。"""

    @pytest.mark.asyncio
    async def test_ack_sent_periodically(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.ACK_INTERVAL = 0.05  # 50ms
        client.start()

        client.on_content_received()
        client.on_content_received()

        await asyncio.sleep(0.15)

        # 应至少发送过一次 ACK
        ack_calls = [
            call for call in ws.send_json.call_args_list
            if isinstance(call[0][0], dict) and call[0][0].get("type") == "ack"
        ]
        assert len(ack_calls) >= 1
        assert ack_calls[0][0][0]["ack"] == 2
        client.stop()

    @pytest.mark.asyncio
    async def test_batch_ack_on_high_throughput(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.ACK_INTERVAL = 10  # 很久才定时 ACK
        client.ACK_BATCH = 5
        client.start()

        for _ in range(5):
            client.on_content_received()

        await asyncio.sleep(0.05)

        # 批量触发应发送 ACK
        ack_calls = [
            call for call in ws.send_json.call_args_list
            if isinstance(call[0][0], dict) and call[0][0].get("type") == "ack"
        ]
        assert len(ack_calls) >= 1
        assert ack_calls[0][0][0]["ack"] == 5
        client.stop()

    @pytest.mark.asyncio
    async def test_on_peer_ack_clears_buffer(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        client.enqueue("json",{"n": 1})
        client.enqueue("json",{"n": 2})
        await asyncio.sleep(0.05)
        assert client.send_buffer.pending_count == 2

        client.on_peer_ack(1)
        assert client.send_buffer.pending_count == 1
        assert client.send_buffer.peer_ack == 1
        client.stop()


class TestClientDeadTimeout:
    """Client 死连接检测。"""

    @pytest.mark.asyncio
    async def test_dead_timeout_enters_buffering(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.DEAD_TIMEOUT = 0.05
        client.start()

        await asyncio.sleep(0.1)
        assert client.state == "buffering"
        client.stop()

    @pytest.mark.asyncio
    async def test_message_resets_dead_timer(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.DEAD_TIMEOUT = 0.1
        client.start()

        # 持续发送内容，不应超时
        for _ in range(5):
            client.on_content_received()
            await asyncio.sleep(0.03)

        assert client.state == "connected"
        client.stop()


class TestClientReplayOnResume:
    """Client 重连后消息重发。"""

    @pytest.mark.asyncio
    async def test_replay_after_resume(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        client.enqueue("json",{"n": 1})
        client.enqueue("json",{"n": 2})
        client.enqueue("json",{"n": 3})
        await asyncio.sleep(0.05)

        client.enter_buffering()

        ws2 = _make_mock_ws()
        ok = client.resume(ws2, last_seq=1)
        assert ok is True

        # 应重发 n=2 和 n=3
        msgs = client.get_replay_messages(1)
        assert len(msgs) == 2
        assert msgs[0] == ("json", {"n": 2})
        assert msgs[1] == ("json", {"n": 3})
        client.stop()

    @pytest.mark.asyncio
    async def test_reset_for_fresh_connection(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        client.enqueue("json",{"n": 1})
        client.on_content_received()
        await asyncio.sleep(0.05)

        ws2 = _make_mock_ws()
        client.reset_for_fresh_connection(ws2)

        assert client.state == "connected"
        assert client.ws is ws2
        assert client.recv_count == 0
        assert client.send_buffer.total_sent == 0
        assert client.send_buffer.pending_count == 0
        client.stop()


class TestClientExpireCallback:
    """Client 过期回调。"""

    @pytest.mark.asyncio
    async def test_expire_callback_called(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.BUFFER_TIMEOUT = 0.03
        client.start()

        results: list[Client] = []
        client.on_expire(lambda c: results.append(c))

        client.enter_buffering()
        await asyncio.sleep(0.1)

        assert len(results) == 1
        assert results[0] is client
        assert client.state == "expired"
        client.stop()


# ===================================================================
# Channel
# ===================================================================


class TestChannel:
    """Channel 消息入队。"""

    def _make_channel(self, ch_id: int, client: Client, session_id: str = "") -> Channel:
        ch = Channel(ch=ch_id, session_id=session_id)
        ChannelTransport.get_or_create(ch)._client = client
        return ch

    @pytest.mark.asyncio
    async def test_enqueue_json_injects_ch(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        ch = self._make_channel(3, client, "s1")
        ch.send_json({"type": "text_delta", "delta": "hi"})

        await asyncio.sleep(0.05)

        ws.send_json.assert_called_with({"ch": 3, "type": "text_delta", "delta": "hi"})
        client.stop()

    @pytest.mark.asyncio
    async def test_enqueue_binary_adds_varint_prefix(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        ch = self._make_channel(1, client, "t1")
        payload = bytes([0x01]) + b"terminal output"
        ch.send_binary(payload)

        await asyncio.sleep(0.05)

        expected = bytes([0x01]) + payload  # varint(1) = 0x01
        ws.send_bytes.assert_called_with(expected)
        client.stop()

    @pytest.mark.asyncio
    async def test_enqueue_binary_large_ch_id(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()

        ch = self._make_channel(300, client, "t1")
        ch.send_binary(b"\x01data")

        await asyncio.sleep(0.05)

        expected = encode_varint(300) + b"\x01data"
        ws.send_bytes.assert_called_with(expected)
        client.stop()

    @pytest.mark.asyncio
    async def test_enqueue_json_thread_safe(self) -> None:
        """从多个线程调用 send_json 不应报错。"""
        import threading

        ws = _make_mock_ws()
        loop = asyncio.get_running_loop()
        client = Client("c1", "w1", ws, loop=loop)
        ch = self._make_channel(1, client, "s1")
        errors: list[Exception] = []

        def enqueue_many() -> None:
            try:
                for i in range(100):
                    ch.send_json({"n": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=enqueue_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # call_soon_threadsafe 需要让事件循环执行已调度的回调
        await asyncio.sleep(0.05)
        assert client._send_queue.qsize() == 400


# ===================================================================
# ChannelManager
# ===================================================================


class TestChannelManager:
    """ChannelManager 频道管理。"""

    @pytest.mark.asyncio
    async def test_open_assigns_sequential_ids(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        cm = ChannelManager()

        ch1 = cm.open(client, "s1")
        ch2 = cm.open(client, "s2")
        ch3 = cm.open(client, "s3")

        assert ch1.ch == 1
        assert ch2.ch == 2
        assert ch3.ch == 3

    @pytest.mark.asyncio
    async def test_close_recycles_id(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        cm = ChannelManager()

        ch1 = cm.open(client, "s1")
        cm.open(client, "s2")
        cm.close(ch1.ch)

        ch3 = cm.open(client, "s3")
        assert ch3.ch == 1

    @pytest.mark.asyncio
    async def test_get_channel(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        cm = ChannelManager()

        ch = cm.open(client, "s1")
        assert cm.get_channel(ch.ch) is ch
        assert cm.get_channel(999) is None

    @pytest.mark.asyncio
    async def test_get_channels_for_session(self) -> None:
        ws1 = _make_mock_ws()
        ws2 = _make_mock_ws()
        c1 = Client("c1", "w1", ws1)
        c2 = Client("c2", "w1", ws2)
        cm = ChannelManager()

        cm.open(c1, "s1")
        cm.open(c2, "s1")
        cm.open(c1, "s2")

        s1_channels = cm.get_channels_for_session("s1")
        assert len(s1_channels) == 2
        assert all(ch.session_id == "s1" for ch in s1_channels)

        s2_channels = cm.get_channels_for_session("s2")
        assert len(s2_channels) == 1

        assert cm.get_channels_for_session("nonexistent") == []

    @pytest.mark.asyncio
    async def test_get_channels_for_client(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        cm = ChannelManager()

        cm.open(client, "s1")
        cm.open(client, "s2")

        channels = cm.get_channels_for_client("c1")
        assert len(channels) == 2
        assert cm.get_channels_for_client("nonexistent") == []

    @pytest.mark.asyncio
    async def test_close_all_for_client(self) -> None:
        ws1 = _make_mock_ws()
        ws2 = _make_mock_ws()
        c1 = Client("c1", "w1", ws1)
        c2 = Client("c2", "w1", ws2)
        cm = ChannelManager()

        cm.open(c1, "s1")
        cm.open(c1, "s2")
        cm.open(c2, "s1")

        closed = cm.close_all_for_client(c1)
        assert len(closed) == 2

        assert cm.get_channels_for_client("c1") == []
        assert len(cm.get_channels_for_client("c2")) == 1
        assert len(cm.get_channels_for_session("s1")) == 1

    @pytest.mark.asyncio
    async def test_close_returns_none_for_invalid(self) -> None:
        cm = ChannelManager()
        assert cm.close(999) is None

    @pytest.mark.asyncio
    async def test_close_all_for_client_recycles_ids(self) -> None:
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        cm = ChannelManager()

        cm.open(client, "s1")
        cm.open(client, "s2")
        cm.close_all_for_client(client)

        ws2 = _make_mock_ws()
        c2 = Client("c2", "w1", ws2)
        ch = cm.open(c2, "s3")
        assert ch.ch == 1

    @pytest.mark.asyncio
    async def test_multi_client_isolation(self) -> None:
        ws1 = _make_mock_ws()
        ws2 = _make_mock_ws()
        c1 = Client("c1", "w1", ws1)
        c2 = Client("c2", "w1", ws2)
        cm = ChannelManager()

        ch1 = cm.open(c1, "s1")
        ch2 = cm.open(c2, "s1")

        assert ch1.ch != ch2.ch
        assert len(cm.get_channels_for_session("s1")) == 2

    @pytest.mark.asyncio
    async def test_thread_safety_concurrent_open_close(self) -> None:
        """并发 open/close 不应导致数据不一致。"""
        import threading

        loop = asyncio.get_running_loop()
        cm = ChannelManager()
        errors: list[Exception] = []

        def worker(client_id: str) -> None:
            try:
                ws = _make_mock_ws()
                client = Client(client_id, "w1", ws, loop=loop)
                channels = []
                for i in range(50):
                    ch = cm.open(client, f"s{i}")
                    channels.append(ch)
                for ch in channels:
                    cm.close(ch.ch)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"c{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        for i in range(200):
            assert cm.get_channel(i + 1) is None


# ===================================================================
# Passive close (channel.closed) scenarios
# ===================================================================


class TestPassiveClose:
    """session 删除 / client 过期时自动关闭 channel 的场景。"""

    @pytest.mark.asyncio
    async def test_session_delete_closes_channels_and_pushes_event(self) -> None:
        """session 删除时，关联 channel 应被关闭并推送 channel.closed 事件。"""
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()
        cm = ChannelManager()

        ch1 = cm.open(client, session_id="s1")
        ch2 = cm.open(client, session_id="s1")
        ch3 = cm.open(client, session_id="s2")  # different session

        # Simulate _close_channels_for_session logic
        channels = cm.get_channels_for_session("s1")
        for channel in channels:
            cm.close(channel.ch)
            client.enqueue("json",{
                "type": "event",
                "event": "channel.closed",
                "closed_ch": channel.ch,
                "reason": "session_deleted",
            })

        await asyncio.sleep(0.05)

        # ch1 and ch2 should be closed
        assert cm.get_channel(ch1.ch) is None
        assert cm.get_channel(ch2.ch) is None
        # ch3 (different session) should still be open
        assert cm.get_channel(ch3.ch) is not None

        # Verify channel.closed events were sent
        sent_calls = [
            call.args[0] for call in ws.send_json.call_args_list
            if isinstance(call.args[0], dict) and call.args[0].get("event") == "channel.closed"
        ]
        assert len(sent_calls) == 2
        closed_chs = {c["closed_ch"] for c in sent_calls}
        assert closed_chs == {ch1.ch, ch2.ch}
        for c in sent_calls:
            assert c["reason"] == "session_deleted"

        client.stop()

    @pytest.mark.asyncio
    async def test_client_expire_closes_all_channels(self) -> None:
        """client 过期时，所有 channel 应被关闭（无推送，ws 已断）。"""
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.BUFFER_TIMEOUT = 0.01
        client.start()
        cm = ChannelManager()

        ch1 = cm.open(client, session_id="s1")
        ch2 = cm.open(client, session_id="s2")

        def on_expire(c: Client) -> None:
            cm.close_all_for_client(c)

        client.on_expire(on_expire)
        client.enter_buffering()

        await asyncio.sleep(0.05)

        assert client.state == "expired"
        assert cm.get_channel(ch1.ch) is None
        assert cm.get_channel(ch2.ch) is None

        client.stop()

    @pytest.mark.asyncio
    async def test_session_restart_closes_channels_with_restart_reason(self) -> None:
        """session restart 时，channel.closed 应携带 session_restarted reason。"""
        ws = _make_mock_ws()
        client = Client("c1", "w1", ws)
        client.start()
        cm = ChannelManager()

        ch = cm.open(client, session_id="s1")

        channels = cm.get_channels_for_session("s1")
        for channel in channels:
            cm.close(channel.ch)
            client.enqueue("json",{
                "type": "event",
                "event": "channel.closed",
                "closed_ch": channel.ch,
                "reason": "session_restarted",
            })

        await asyncio.sleep(0.05)

        assert cm.get_channel(ch.ch) is None
        sent_calls = [
            call.args[0] for call in ws.send_json.call_args_list
            if isinstance(call.args[0], dict) and call.args[0].get("event") == "channel.closed"
        ]
        assert len(sent_calls) == 1
        assert sent_calls[0]["reason"] == "session_restarted"

        client.stop()
