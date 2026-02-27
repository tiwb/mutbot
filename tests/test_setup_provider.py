"""SetupProvider 单元测试

涵盖：
- 状态机各状态转换
- 模型发现（fetch 成功 / 失败 / chat_filter）
- 模型选择（编号解析、all、a 展开、手动输入）
- Sync → Async generator adapter
- send() 代理（sync/async provider 兼容）
- 配置构建与合并
- 模型优先级排序
"""

from __future__ import annotations

import json

import pytest

from mutagent.messages import Message, Response, StreamEvent
from mutbot.builtins.setup_provider import (
    SetupProvider,
    _model_family,
    _prioritize_models,
    _wrap_sync_iter,
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


async def _send(provider: SetupProvider, text: str) -> list[StreamEvent]:
    """向 provider 发送一条用户消息，收集响应事件。"""
    messages = [Message(role="user", content=text)]
    return await _collect_events(
        provider.send("setup-wizard", messages, [])
    )


def _get_text(events: list[StreamEvent]) -> str:
    """提取事件中的所有 text_delta 文本。"""
    return "".join(e.text for e in events if e.type == "text_delta" and e.text)


# ---------------------------------------------------------------------------
# 状态机 — 基本转换
# ---------------------------------------------------------------------------

class TestStateMachine:
    """测试 SetupProvider 状态机的各状态转换。"""

    @pytest.mark.asyncio
    async def test_welcome_to_await_choice(self):
        """WELCOME → AWAIT_CHOICE，显示欢迎消息和选项。"""
        p = SetupProvider()
        events = await _send(p, "__setup__")
        assert p._state == "AWAIT_CHOICE"
        text = _get_text(events)
        assert "GitHub Copilot" in text
        assert "Anthropic" in text
        assert "OpenAI" in text

    @pytest.mark.asyncio
    async def test_choice_anthropic(self):
        """选择 2 → AWAIT_KEY（anthropic）。"""
        p = SetupProvider()
        p._state = "AWAIT_CHOICE"
        events = await _send(p, "2")
        assert p._state == "AWAIT_KEY"
        assert p._context["provider_type"] == "anthropic"
        assert "API key" in _get_text(events)

    @pytest.mark.asyncio
    async def test_choice_openai(self):
        """选择 3 → AWAIT_KEY（openai）。"""
        p = SetupProvider()
        p._state = "AWAIT_CHOICE"
        events = await _send(p, "3")
        assert p._state == "AWAIT_KEY"
        assert p._context["provider_type"] == "openai"

    @pytest.mark.asyncio
    async def test_choice_custom_anthropic(self):
        """选择 4 → AWAIT_CUSTOM_URL（anthropic 协议）。"""
        p = SetupProvider()
        p._state = "AWAIT_CHOICE"
        events = await _send(p, "4")
        assert p._state == "AWAIT_CUSTOM_URL"
        assert p._context["protocol"] == "anthropic"

    @pytest.mark.asyncio
    async def test_choice_custom_openai(self):
        """选择 5 → AWAIT_CUSTOM_URL（openai 协议）。"""
        p = SetupProvider()
        p._state = "AWAIT_CHOICE"
        events = await _send(p, "5")
        assert p._state == "AWAIT_CUSTOM_URL"
        assert p._context["protocol"] == "openai"

    @pytest.mark.asyncio
    async def test_choice_invalid_stays(self):
        """无效选项保持在 AWAIT_CHOICE。"""
        p = SetupProvider()
        p._state = "AWAIT_CHOICE"
        events = await _send(p, "99")
        assert p._state == "AWAIT_CHOICE"
        assert "1-5" in _get_text(events)

    @pytest.mark.asyncio
    async def test_cancel_from_await_key(self):
        """AWAIT_KEY 输入 cancel → 回到 AWAIT_CHOICE。"""
        p = SetupProvider()
        p._state = "AWAIT_KEY"
        p._context["provider_type"] = "anthropic"
        events = await _send(p, "cancel")
        assert p._state == "AWAIT_CHOICE"
        assert p._context == {}

    @pytest.mark.asyncio
    async def test_cancel_from_custom_url(self):
        """AWAIT_CUSTOM_URL 输入 cancel → 回到 AWAIT_CHOICE。"""
        p = SetupProvider()
        p._state = "AWAIT_CUSTOM_URL"
        p._context["protocol"] = "openai"
        events = await _send(p, "cancel")
        assert p._state == "AWAIT_CHOICE"

    @pytest.mark.asyncio
    async def test_cancel_from_custom_key(self):
        """AWAIT_CUSTOM_KEY 输入 cancel → 回到 AWAIT_CHOICE。"""
        p = SetupProvider()
        p._state = "AWAIT_CUSTOM_KEY"
        p._context = {"base_url": "http://x", "protocol": "openai"}
        events = await _send(p, "cancel")
        assert p._state == "AWAIT_CHOICE"

    @pytest.mark.asyncio
    async def test_cancel_from_model_selection(self):
        """AWAIT_MODEL 输入 cancel → 回到 AWAIT_CHOICE。"""
        p = SetupProvider()
        p._state = "AWAIT_MODEL"
        p._context = {"available_models": ["a"]}
        events = await _send(p, "cancel")
        assert p._state == "AWAIT_CHOICE"

    @pytest.mark.asyncio
    async def test_copilot_polling_interrupted(self):
        """COPILOT_POLLING 状态被取消后重新进入 → AWAIT_CHOICE。"""
        p = SetupProvider()
        p._state = "COPILOT_POLLING"
        events = await _send(p, "anything")
        assert p._state == "AWAIT_CHOICE"
        assert "interrupted" in _get_text(events).lower()


# ---------------------------------------------------------------------------
# API Key 流程
# ---------------------------------------------------------------------------

class TestApiKeyFlow:
    """测试 Standard Anthropic / OpenAI API Key 流程。"""

    @pytest.mark.asyncio
    async def test_anthropic_key_goes_to_model_selection(self):
        """Anthropic key 输入后直接进入 AWAIT_MODEL（硬编码模型列表）。"""
        p = SetupProvider()
        p._state = "AWAIT_KEY"
        p._context["provider_type"] = "anthropic"
        events = await _send(p, "sk-ant-test-key")

        assert p._state == "AWAIT_MODEL"
        assert p._context["auth_token"] == "sk-ant-test-key"
        models = p._context["available_models"]
        assert "claude-sonnet-4" in models
        assert "claude-haiku-4.5" in models
        assert "claude-opus-4" in models
        # 响应包含模型列表
        text = _get_text(events)
        assert "claude-sonnet-4" in text

    @pytest.mark.asyncio
    async def test_openai_key_fetch_success(self, monkeypatch):
        """OpenAI key → fetch 成功 → AWAIT_MODEL 展示 fetched 模型。"""
        p = SetupProvider()
        p._state = "AWAIT_KEY"
        p._context["provider_type"] = "openai"

        async def mock_fetch(base_url, api_key, *, chat_filter=False):
            return ["gpt-4.1", "gpt-4.1-mini", "o3-mini"]

        monkeypatch.setattr(p, "_fetch_models_async", mock_fetch)
        events = await _send(p, "sk-test-key")

        assert p._state == "AWAIT_MODEL"
        assert p._context["available_models"] == ["gpt-4.1", "gpt-4.1-mini", "o3-mini"]

    @pytest.mark.asyncio
    async def test_openai_key_fetch_fails_fallback(self, monkeypatch):
        """OpenAI key → fetch 失败 → fallback 硬编码模型 → AWAIT_MODEL。"""
        p = SetupProvider()
        p._state = "AWAIT_KEY"
        p._context["provider_type"] = "openai"

        async def mock_fetch(base_url, api_key, *, chat_filter=False):
            return []

        monkeypatch.setattr(p, "_fetch_models_async", mock_fetch)
        events = await _send(p, "sk-test-key")

        assert p._state == "AWAIT_MODEL"
        assert "gpt-4.1" in p._context["available_models"]
        # 应提示 fallback
        assert "Could not fetch" in _get_text(events)


# ---------------------------------------------------------------------------
# Custom API 流程
# ---------------------------------------------------------------------------

class TestCustomApiFlow:
    """测试 Custom API 流程（URL → Key → Model）。"""

    @pytest.mark.asyncio
    async def test_url_accepted(self):
        """有效 URL → AWAIT_CUSTOM_KEY。"""
        p = SetupProvider()
        p._state = "AWAIT_CUSTOM_URL"
        p._context["protocol"] = "openai"
        events = await _send(p, "https://api.example.com/v1")
        assert p._state == "AWAIT_CUSTOM_KEY"
        assert p._context["base_url"] == "https://api.example.com/v1"

    @pytest.mark.asyncio
    async def test_invalid_url_rejected(self):
        """无效 URL → 停留在 AWAIT_CUSTOM_URL。"""
        p = SetupProvider()
        p._state = "AWAIT_CUSTOM_URL"
        p._context["protocol"] = "openai"
        events = await _send(p, "not-a-url")
        assert p._state == "AWAIT_CUSTOM_URL"

    @pytest.mark.asyncio
    async def test_key_with_models_found(self, monkeypatch):
        """Custom key → fetch 成功 → AWAIT_MODEL。"""
        p = SetupProvider()
        p._state = "AWAIT_CUSTOM_KEY"
        p._context = {"base_url": "https://api.example.com", "protocol": "openai"}

        async def mock_fetch(base_url, api_key, *, chat_filter=False):
            return ["model-a", "model-b"]

        monkeypatch.setattr(p, "_fetch_models_async", mock_fetch)
        events = await _send(p, "my-api-key")

        assert p._state == "AWAIT_MODEL"
        assert p._context["available_models"] == ["model-a", "model-b"]

    @pytest.mark.asyncio
    async def test_key_with_no_models(self, monkeypatch):
        """Custom key → fetch 失败 → AWAIT_MANUAL_MODEL。"""
        p = SetupProvider()
        p._state = "AWAIT_CUSTOM_KEY"
        p._context = {"base_url": "https://api.example.com", "protocol": "openai"}

        async def mock_fetch(base_url, api_key, *, chat_filter=False):
            return []

        monkeypatch.setattr(p, "_fetch_models_async", mock_fetch)
        events = await _send(p, "my-api-key")

        assert p._state == "AWAIT_MANUAL_MODEL"
        assert "manually" in _get_text(events).lower() or "manual" in _get_text(events).lower()


# ---------------------------------------------------------------------------
# 模型选择
# ---------------------------------------------------------------------------

class TestModelSelection:
    """测试 AWAIT_MODEL 状态下的模型选择逻辑。"""

    def _setup_provider(self) -> SetupProvider:
        p = SetupProvider()
        p._state = "AWAIT_MODEL"
        p._context = {
            "provider_type": "anthropic",
            "auth_token": "sk-test",
            "available_models": [
                "claude-sonnet-4", "claude-haiku-4.5", "claude-opus-4",
            ],
        }
        return p

    @pytest.mark.asyncio
    async def test_select_by_number(self, monkeypatch):
        """输入编号选择模型。"""
        p = self._setup_provider()
        monkeypatch.setattr(p, "_activate", _mock_activate)
        events = await _send(p, "1,3")
        assert p._context["selected_models"] == ["claude-sonnet-4", "claude-opus-4"]

    @pytest.mark.asyncio
    async def test_select_single(self, monkeypatch):
        """输入单个编号。"""
        p = self._setup_provider()
        monkeypatch.setattr(p, "_activate", _mock_activate)
        events = await _send(p, "2")
        assert p._context["selected_models"] == ["claude-haiku-4.5"]

    @pytest.mark.asyncio
    async def test_select_all(self, monkeypatch):
        """输入 all 选择全部。"""
        p = self._setup_provider()
        monkeypatch.setattr(p, "_activate", _mock_activate)
        events = await _send(p, "all")
        assert p._context["selected_models"] == [
            "claude-sonnet-4", "claude-haiku-4.5", "claude-opus-4",
        ]

    @pytest.mark.asyncio
    async def test_select_by_name(self, monkeypatch):
        """直接输入模型名。"""
        p = self._setup_provider()
        monkeypatch.setattr(p, "_activate", _mock_activate)
        events = await _send(p, "custom-model-id")
        assert p._context["selected_models"] == ["custom-model-id"]

    @pytest.mark.asyncio
    async def test_select_mixed(self, monkeypatch):
        """混合编号和名称。"""
        p = self._setup_provider()
        monkeypatch.setattr(p, "_activate", _mock_activate)
        events = await _send(p, "1, custom-model")
        assert p._context["selected_models"] == ["claude-sonnet-4", "custom-model"]

    @pytest.mark.asyncio
    async def test_empty_selection_stays(self):
        """空输入保持在 AWAIT_MODEL。"""
        p = self._setup_provider()
        events = await _send(p, "")
        assert p._state == "AWAIT_MODEL"

    @pytest.mark.asyncio
    async def test_show_all_models(self):
        """超过 10 个模型时，输入 a 展开全部。"""
        p = SetupProvider()
        p._state = "AWAIT_MODEL"
        models = [f"model-{i}" for i in range(15)]
        p._context = {
            "provider_type": "openai",
            "auth_token": "sk-test",
            "available_models": models,
        }
        events = await _send(p, "a")
        assert p._state == "AWAIT_MODEL"  # stays（展示全部后等选择）
        assert p._context.get("show_all") is True
        text = _get_text(events)
        assert "model-14" in text  # 最后一个模型可见

    @pytest.mark.asyncio
    async def test_dedup_selection(self, monkeypatch):
        """重复选择去重。"""
        p = self._setup_provider()
        monkeypatch.setattr(p, "_activate", _mock_activate)
        events = await _send(p, "1,1,2")
        assert p._context["selected_models"] == [
            "claude-sonnet-4", "claude-haiku-4.5",
        ]


class TestManualModel:
    """测试 AWAIT_MANUAL_MODEL 状态。"""

    @pytest.mark.asyncio
    async def test_manual_model_input(self, monkeypatch):
        """输入模型 ID → activate。"""
        p = SetupProvider()
        p._state = "AWAIT_MANUAL_MODEL"
        p._context = {
            "base_url": "https://api.example.com",
            "auth_token": "key",
            "protocol": "openai",
        }
        monkeypatch.setattr(p, "_activate", _mock_activate)
        events = await _send(p, "my-custom-model")
        assert p._context["selected_models"] == ["my-custom-model"]

    @pytest.mark.asyncio
    async def test_empty_model_stays(self):
        """空输入保持在 AWAIT_MANUAL_MODEL。"""
        p = SetupProvider()
        p._state = "AWAIT_MANUAL_MODEL"
        p._context = {"base_url": "http://x", "auth_token": "k", "protocol": "openai"}
        events = await _send(p, "")
        assert p._state == "AWAIT_MANUAL_MODEL"


async def _mock_activate(provider: str) -> str:
    return "✅ Done"


# ---------------------------------------------------------------------------
# Sync → Async adapter
# ---------------------------------------------------------------------------

class TestWrapSyncIter:
    """测试 _wrap_sync_iter 同步→异步 generator 适配。"""

    @pytest.mark.asyncio
    async def test_basic_iteration(self):
        """正常迭代同步 generator。"""
        def gen():
            yield 1
            yield 2
            yield 3

        result = []
        async for item in _wrap_sync_iter(gen()):
            result.append(item)
        assert result == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_empty_generator(self):
        """空 generator。"""
        def gen():
            return
            yield  # noqa: unreachable — makes it a generator

        result = []
        async for item in _wrap_sync_iter(gen()):
            result.append(item)
        assert result == []

    @pytest.mark.asyncio
    async def test_exception_propagation(self):
        """同步 generator 抛异常 → 异步侧收到相同异常。"""
        def gen():
            yield 1
            raise ValueError("test error")

        result = []
        with pytest.raises(ValueError, match="test error"):
            async for item in _wrap_sync_iter(gen()):
                result.append(item)
        assert result == [1]

    @pytest.mark.asyncio
    async def test_stream_events(self):
        """StreamEvent 对象正确传递。"""
        def gen():
            yield StreamEvent(type="text_delta", text="hello")
            yield StreamEvent(type="response_done", response=Response(
                message=Message(role="assistant", content="hello"),
                stop_reason="end_turn",
            ))

        events = []
        async for item in _wrap_sync_iter(gen()):
            events.append(item)
        assert len(events) == 2
        assert events[0].type == "text_delta"
        assert events[0].text == "hello"
        assert events[1].type == "response_done"


# ---------------------------------------------------------------------------
# send() 代理 — sync/async 兼容
# ---------------------------------------------------------------------------

class TestSendProxy:
    """测试配置完成后 send() 代理到真实 provider。"""

    @pytest.mark.asyncio
    async def test_proxy_async_provider(self):
        """代理 async generator provider。"""
        p = SetupProvider()

        class AsyncProvider:
            async def send(self, model, messages, tools,
                           system_prompt="", stream=True):
                yield StreamEvent(type="text_delta", text="async-hello")
                yield StreamEvent(type="response_done", response=Response(
                    message=Message(role="assistant", content="async-hello"),
                    stop_reason="end_turn",
                ))

        p._real_provider = AsyncProvider()
        p._real_model = "test-model"

        events = await _collect_events(
            p.send("m", [Message(role="user", content="hi")], [])
        )
        assert len(events) == 2
        assert events[0].text == "async-hello"

    @pytest.mark.asyncio
    async def test_proxy_sync_provider(self):
        """代理 sync generator provider（如 CopilotProvider）。"""
        p = SetupProvider()

        class SyncProvider:
            def send(self, model, messages, tools,
                     system_prompt="", stream=True):
                yield StreamEvent(type="text_delta", text="sync-hello")
                yield StreamEvent(type="response_done", response=Response(
                    message=Message(role="assistant", content="sync-hello"),
                    stop_reason="end_turn",
                ))

        p._real_provider = SyncProvider()
        p._real_model = "test-model"

        events = await _collect_events(
            p.send("m", [Message(role="user", content="hi")], [])
        )
        assert len(events) == 2
        assert events[0].text == "sync-hello"


# ---------------------------------------------------------------------------
# 配置构建
# ---------------------------------------------------------------------------

class TestConfigBuild:
    """测试 _build_provider_config 配置生成。"""

    def test_copilot_config(self):
        p = SetupProvider()
        p._context = {
            "github_token": "gho_test",
            "selected_models": ["claude-sonnet-4", "gpt-4.1"],
        }
        config = p._build_provider_config("copilot")
        assert config["default_model"] == "claude-sonnet-4"
        prov = config["providers"]["copilot"]
        assert prov["github_token"] == "gho_test"
        assert prov["models"] == ["claude-sonnet-4", "gpt-4.1"]

    def test_anthropic_config(self):
        p = SetupProvider()
        p._context = {
            "auth_token": "sk-ant-test",
            "selected_models": ["claude-sonnet-4"],
        }
        config = p._build_provider_config("anthropic")
        prov = config["providers"]["anthropic"]
        assert prov["auth_token"] == "sk-ant-test"
        assert prov["base_url"] == "https://api.anthropic.com"
        assert prov["models"] == ["claude-sonnet-4"]

    def test_openai_config(self):
        p = SetupProvider()
        p._context = {
            "auth_token": "sk-test",
            "selected_models": ["gpt-4.1", "gpt-4.1-mini"],
        }
        config = p._build_provider_config("openai")
        prov = config["providers"]["openai"]
        assert prov["provider"] == "OpenAIProvider"
        assert prov["models"] == ["gpt-4.1", "gpt-4.1-mini"]

    def test_custom_anthropic_config(self):
        p = SetupProvider()
        p._context = {
            "base_url": "https://api.example.com",
            "auth_token": "key",
            "protocol": "anthropic",
            "selected_models": ["my-model"],
        }
        config = p._build_provider_config("custom")
        prov = config["providers"]["custom"]
        assert prov["provider"] == "AnthropicProvider"
        assert prov["base_url"] == "https://api.example.com"

    def test_custom_openai_config(self):
        p = SetupProvider()
        p._context = {
            "base_url": "https://api.example.com/v1",
            "auth_token": "key",
            "protocol": "openai",
            "selected_models": ["model-x"],
        }
        config = p._build_provider_config("custom")
        prov = config["providers"]["custom"]
        assert prov["provider"] == "OpenAIProvider"


# ---------------------------------------------------------------------------
# 配置保存与合并
# ---------------------------------------------------------------------------

class TestSaveConfig:
    """测试 _save_config 配置文件写入与合并。"""

    def test_save_new_config(self, tmp_path, monkeypatch):
        """全新写入配置文件。"""
        import mutbot.builtins.setup_provider as sp
        monkeypatch.setattr(sp, "MUTBOT_CONFIG_PATH", tmp_path / "config.json")
        monkeypatch.setattr(sp, "MUTBOT_USER_DIR", tmp_path)

        p = SetupProvider()
        p._save_config({
            "default_model": "test-model",
            "providers": {"test": {"provider": "T", "models": ["test-model"]}},
        })

        saved = json.loads((tmp_path / "config.json").read_text())
        assert saved["default_model"] == "test-model"
        assert "test" in saved["providers"]

    def test_merge_preserves_existing(self, tmp_path, monkeypatch):
        """合并写入时保留已有 providers。"""
        import mutbot.builtins.setup_provider as sp
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "default_model": "old-model",
            "providers": {"existing": {"provider": "Old", "models": ["old"]}},
        }))
        monkeypatch.setattr(sp, "MUTBOT_CONFIG_PATH", config_path)
        monkeypatch.setattr(sp, "MUTBOT_USER_DIR", tmp_path)

        p = SetupProvider()
        p._save_config({
            "default_model": "new-model",
            "providers": {"new": {"provider": "New", "models": ["new"]}},
        })

        saved = json.loads(config_path.read_text())
        assert saved["default_model"] == "new-model"
        assert "existing" in saved["providers"]
        assert "new" in saved["providers"]


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
