"""Tests for mutbot config system (runtime/config.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mutagent.builtins  # noqa: F401  -- register @impl
from mutagent.config import Config
from mutbot.runtime.config import MutbotConfig, load_mutbot_config


# ---------------------------------------------------------------------------
# MutbotConfig tests
# ---------------------------------------------------------------------------

class TestMutbotConfig:

    def _make_config(self, tmp_path: Path, data: dict | None = None) -> MutbotConfig:
        config_path = tmp_path / "config.json"
        if data is not None:
            config_path.write_text(json.dumps(data), encoding="utf-8")
            mtime = config_path.stat().st_mtime
        else:
            mtime = 0.0
        return MutbotConfig(
            _data=data or {},
            _listeners=[],
            _config_path=config_path,
            _last_write_mtime=mtime,
        )

    def test_isinstance_config(self, tmp_path):
        config = self._make_config(tmp_path)
        assert isinstance(config, Config)

    def test_get_simple(self, tmp_path):
        config = self._make_config(tmp_path, {"default_model": "claude-sonnet-4"})
        assert config.get("default_model") == "claude-sonnet-4"

    def test_get_dot_path(self, tmp_path):
        config = self._make_config(tmp_path, {
            "providers": {"anthropic": {"auth_token": "sk-test"}},
        })
        assert config.get("providers.anthropic.auth_token") == "sk-test"

    def test_get_default(self, tmp_path):
        config = self._make_config(tmp_path)
        assert config.get("missing", default="fallback") == "fallback"

    def test_set_persists_to_file(self, tmp_path):
        config = self._make_config(tmp_path, {})
        config.set("default_model", "gpt-4.1")
        # 内存中已更新
        assert config.get("default_model") == "gpt-4.1"
        # 文件已写入
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["default_model"] == "gpt-4.1"

    def test_set_dot_path(self, tmp_path):
        config = self._make_config(tmp_path, {})
        config.set("providers.openai.auth_token", "sk-new")
        assert config.get("providers.openai.auth_token") == "sk-new"

    def test_set_triggers_on_change(self, tmp_path):
        config = self._make_config(tmp_path, {})
        events = []
        config.on_change("providers.**", lambda e: events.append(e))
        config.set("providers.anthropic", {"auth_token": "sk-test"})
        assert len(events) == 1
        assert events[0].key == "providers.anthropic"
        assert events[0].config is config

    def test_on_change_dispose(self, tmp_path):
        config = self._make_config(tmp_path, {})
        events = []
        disposable = config.on_change("**", lambda e: events.append(e))
        config.set("foo", "bar")
        assert len(events) == 1
        disposable.dispose()
        config.set("foo", "baz")
        assert len(events) == 1  # no new event

    def test_reload_detects_changes(self, tmp_path):
        config = self._make_config(tmp_path, {"default_model": "old"})
        events = []
        config.on_change("**", lambda e: events.append(e))
        # 外部修改文件（重置 _last_write_mtime 模拟非自身写入）
        config._last_write_mtime = 0.0
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"default_model": "new"}), encoding="utf-8")
        config.reload()
        assert config.get("default_model") == "new"
        assert len(events) == 1
        assert events[0].key == "default_model"

    def test_reload_skips_own_write(self, tmp_path):
        config = self._make_config(tmp_path, {})
        events = []
        config.on_change("**", lambda e: events.append(e))
        config.set("foo", "bar")
        events.clear()
        # reload 应跳过自己的写入
        config.reload()
        assert len(events) == 0

    def test_update_all(self, tmp_path):
        config = self._make_config(tmp_path, {"a": 1, "b": 2})
        events = []
        config.on_change("**", lambda e: events.append(e))
        config.update_all({"a": 1, "b": 3, "c": 4}, source="wizard")
        assert config.get("b") == 3
        assert config.get("c") == 4
        # a 没变，b 和 c 有变
        changed_keys = {e.key for e in events}
        assert "b" in changed_keys
        assert "c" in changed_keys
        assert "a" not in changed_keys

    def test_env_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_TOKEN", "expanded-value")
        config = self._make_config(tmp_path, {"token": "${TEST_TOKEN}"})
        assert config.get("token") == "expanded-value"


# ---------------------------------------------------------------------------
# load_mutbot_config() tests
# ---------------------------------------------------------------------------

class TestLoadMutbotConfig:

    def test_returns_mutbot_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mutbot.runtime.config.MUTBOT_USER_DIR", tmp_path)
        config = load_mutbot_config()
        assert isinstance(config, MutbotConfig)

    def test_loads_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mutbot.runtime.config.MUTBOT_USER_DIR", tmp_path)
        (tmp_path / "config.json").write_text(json.dumps({
            "default_model": "test-model",
        }), encoding="utf-8")
        config = load_mutbot_config()
        assert config.get("default_model") == "test-model"


# ---------------------------------------------------------------------------
# Provider validation tests
# ---------------------------------------------------------------------------

class TestProviderValidation:

    def test_anthropic_requires_auth_token(self):
        from mutagent.builtins.anthropic_provider import AnthropicProvider
        with pytest.raises(ValueError, match="auth_token"):
            AnthropicProvider.from_spec({"model_id": "claude-3"})

    def test_anthropic_accepts_auth_token(self):
        from mutagent.builtins.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider.from_spec({
            "auth_token": "sk-test-key",
            "base_url": "https://api.anthropic.com",
        })
        assert provider.api_key == "sk-test-key"

    def test_openai_requires_auth_token(self):
        from mutagent.builtins.openai_provider import OpenAIProvider
        with pytest.raises(ValueError, match="auth_token"):
            OpenAIProvider.from_spec({"model_id": "gpt-4"})

    def test_openai_accepts_auth_token(self):
        from mutagent.builtins.openai_provider import OpenAIProvider
        provider = OpenAIProvider.from_spec({
            "auth_token": "sk-test-key",
            "base_url": "https://api.openai.com/v1",
        })
        assert provider.api_key == "sk-test-key"
