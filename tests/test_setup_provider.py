"""NullProvider 与 Setup 辅助工具单元测试

涵盖：
- NullProvider：fallback 行为
- 配置构建与合并
- 模型优先级排序
"""

from __future__ import annotations

import pytest

from mutagent.messages import Message, StreamEvent, TextBlock, ToolUseBlock
from mutbot.builtins.config_toolkit import (
    NullProvider,
    _model_family,
    _prioritize_models,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _collect_events(gen) -> list[StreamEvent]:
    """从 async generator 收集所有事件。"""
    events = []
    async for event in gen:
        events.append(event)
    return events


async def _send(provider: NullProvider, text: str) -> list[StreamEvent]:
    """向 provider 发送一条用户消息，收集响应事件。"""
    messages = [Message(role="user", blocks=[TextBlock(text=text)])]
    return await _collect_events(
        provider.send("setup-wizard", messages, [])
    )


def _get_text(events: list[StreamEvent]) -> str:
    """提取事件中的所有 text_delta 文本。"""
    return "".join(e.text for e in events if e.type == "text_delta" and e.text)


# ---------------------------------------------------------------------------
# NullProvider — 引导 + Config-llm tool_use
# ---------------------------------------------------------------------------

class TestNullProviderFallback:
    """测试 NullProvider 返回引导文本 + Config-llm tool_use。"""

    @pytest.mark.asyncio
    async def test_returns_guide_text(self):
        """返回引导文本。"""
        p = NullProvider()
        events = await _send(p, "hello")

        text = _get_text(events)
        assert "设置" in text or "配置" in text or "MutBot" in text

    @pytest.mark.asyncio
    async def test_returns_setup_tool_use(self):
        """response 包含 Config-llm ToolUseBlock。"""
        p = NullProvider()
        events = await _send(p, "hello")

        done_events = [e for e in events if e.type == "response_done"]
        assert len(done_events) == 1
        assert done_events[0].response.stop_reason == "tool_use"
        tool_blocks = [
            b for b in done_events[0].response.message.blocks
            if isinstance(b, ToolUseBlock)
        ]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].name == "Config-llm"


# ---------------------------------------------------------------------------
# 模型优先级排序
# ---------------------------------------------------------------------------

class TestModelPrioritization:
    """测试 ported from CLI 的模型排序逻辑。"""

    def test_model_family(self):
        assert _model_family("gpt-4.1-mini") == "gpt-4.1"
        assert _model_family("o3-mini") == "o3"
        assert _model_family("claude-sonnet-4") == "claude-sonnet-4"
        assert _model_family("gpt-4.1-turbo") == "gpt-4.1"

    def test_prioritize_models_basic(self):
        models = [
            ("gpt-4.1", 100),
            ("gpt-4.1-mini", 100),
            ("gpt-3.5-turbo", 50),
            ("o3", 90),
            ("o3-mini", 90),
        ]
        result = _prioritize_models(models)
        assert isinstance(result, list)
        assert len(result) == 5
        # 所有模型都在结果中
        assert set(result) == {"gpt-4.1", "gpt-4.1-mini", "gpt-3.5-turbo", "o3", "o3-mini"}

    def test_prioritize_empty(self):
        assert _prioritize_models([]) == []

    def test_prioritize_single(self):
        result = _prioritize_models([("gpt-4.1", 100)])
        assert result == ["gpt-4.1"]
