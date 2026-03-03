"""UIContext + UIToolkit 单元测试

涵盖：
- UIContext set_view / wait_event / show / close
- UIContext 注册表与 deliver_event 路由
- UIToolkit lazy UIContext 创建
"""

from __future__ import annotations

import asyncio

import pytest

from mutbot.ui.context import UIContext
from mutbot.ui.events import UIEvent
from mutbot.ui.context_impl import (
    register_context,
    unregister_context,
    deliver_event,
    _active_contexts,
)


# ---------------------------------------------------------------------------
# UIContext 基本行为
# ---------------------------------------------------------------------------

class TestUIContext:
    """UIContext set_view / wait_event / show / close。"""

    def _make_context(self) -> tuple[UIContext, list[dict]]:
        """创建 UIContext，broadcast 记录到 list。"""
        sent: list[dict] = []

        def broadcast(data: dict):
            sent.append(data)

        ctx = UIContext(context_id="test-ctx-1", broadcast=broadcast)
        register_context(ctx)
        return ctx, sent

    def test_set_view_broadcasts(self):
        """set_view 通过 broadcast 发送 ui_view 消息。"""
        ctx, sent = self._make_context()
        try:
            view = {"title": "Test", "components": []}
            ctx.set_view(view)

            assert len(sent) == 1
            assert sent[0]["type"] == "ui_view"
            assert sent[0]["context_id"] == "test-ctx-1"
            assert sent[0]["view"] == view
        finally:
            unregister_context("test-ctx-1")

    @pytest.mark.asyncio
    async def test_wait_event_receives(self):
        """wait_event 接收 deliver_event 投递的事件。"""
        ctx, _ = self._make_context()
        try:
            # 异步投递事件
            async def deliver_later():
                await asyncio.sleep(0.01)
                deliver_event("test-ctx-1", UIEvent(type="submit", data={"x": 1}))

            task = asyncio.create_task(deliver_later())
            event = await ctx.wait_event()
            await task

            assert event.type == "submit"
            assert event.data == {"x": 1}
        finally:
            unregister_context("test-ctx-1")

    @pytest.mark.asyncio
    async def test_wait_event_filters_type(self):
        """wait_event 按 type 过滤。"""
        ctx, _ = self._make_context()
        try:
            async def deliver_later():
                await asyncio.sleep(0.01)
                # 先发一个 change（不匹配）
                deliver_event("test-ctx-1", UIEvent(type="change", data={"a": 1}))
                await asyncio.sleep(0.01)
                # 再发一个 submit（匹配）
                deliver_event("test-ctx-1", UIEvent(type="submit", data={"b": 2}))

            task = asyncio.create_task(deliver_later())
            event = await ctx.wait_event(type="submit")
            await task

            assert event.type == "submit"
            assert event.data == {"b": 2}
        finally:
            unregister_context("test-ctx-1")

    @pytest.mark.asyncio
    async def test_show_combines_set_view_and_wait(self):
        """show = set_view + wait_event(type='submit')。"""
        ctx, sent = self._make_context()
        try:
            async def deliver_later():
                await asyncio.sleep(0.01)
                deliver_event("test-ctx-1", UIEvent(type="submit", data={"name": "test"}))

            task = asyncio.create_task(deliver_later())
            result = await ctx.show({"title": "Form", "components": []})
            await task

            assert result == {"name": "test"}
            assert sent[0]["type"] == "ui_view"
        finally:
            unregister_context("test-ctx-1")

    def test_close_sends_ui_close(self):
        """close 发送 ui_close 消息并注销。"""
        ctx, sent = self._make_context()

        ctx.close(final_view={"title": "Done", "components": []})

        assert len(sent) == 1
        assert sent[0]["type"] == "ui_close"
        assert sent[0]["context_id"] == "test-ctx-1"
        assert sent[0]["final_view"]["title"] == "Done"

        # 已从注册表中移除
        assert "test-ctx-1" not in _active_contexts

    def test_close_idempotent(self):
        """多次 close 不报错。"""
        ctx, sent = self._make_context()
        ctx.close()
        ctx.close()
        assert len(sent) == 1  # 只发一次

    def test_set_view_after_close_ignored(self):
        """close 后 set_view 被忽略。"""
        ctx, sent = self._make_context()
        ctx.close()
        ctx.set_view({"components": []})
        assert len(sent) == 1  # 只有 close 消息


# ---------------------------------------------------------------------------
# deliver_event 路由
# ---------------------------------------------------------------------------

class TestDeliverEvent:
    """测试 deliver_event 路由正确性。"""

    def test_delivers_to_registered_context(self):
        """deliver_event 找到对应 UIContext。"""
        ctx, _ = TestUIContext()._make_context()
        try:
            result = deliver_event("test-ctx-1", UIEvent(type="test", data={}))
            assert result is True
        finally:
            unregister_context("test-ctx-1")

    def test_returns_false_for_unknown(self):
        """deliver_event 找不到 UIContext 返回 False。"""
        result = deliver_event("nonexistent", UIEvent(type="test", data={}))
        assert result is False


# ---------------------------------------------------------------------------
# UIToolkit
# ---------------------------------------------------------------------------

class TestUIToolkit:
    """测试 UIToolkit lazy UIContext 创建。"""

    def test_ui_property_raises_without_owner(self):
        """无 owner 时访问 .ui 报错。"""
        from mutbot.ui.toolkit import UIToolkit

        toolkit = UIToolkit()
        with pytest.raises(RuntimeError, match="owner not set"):
            _ = toolkit.ui

    def test_ui_property_raises_without_tool_call(self):
        """有 owner 但无 _current_tool_call 时报错。"""
        from mutbot.ui.toolkit import UIToolkit
        from mutagent.tools import ToolSet

        toolkit = UIToolkit()
        ts = ToolSet()
        toolkit.owner = ts
        with pytest.raises(RuntimeError, match="outside of dispatch"):
            _ = toolkit.ui
