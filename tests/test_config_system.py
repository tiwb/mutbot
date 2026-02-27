"""Tests for mutbot config system (runtime/config.py and setup_provider)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import mutagent.builtins  # noqa: F401  -- register @impl
from mutagent.config import Config
from mutbot.runtime.config import MUTBOT_CONFIG_FILES, load_mutbot_config
from mutbot.builtins.setup_provider import _write_config, MUTBOT_CONFIG_PATH


# ---------------------------------------------------------------------------
# load_mutbot_config() tests
# ---------------------------------------------------------------------------

class TestLoadMutbotConfig:

    def test_returns_config_instance(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = load_mutbot_config()
        assert isinstance(config, Config)

    def test_two_layer_merge_priority(self, tmp_path, monkeypatch):
        """mutbot 两层配置合并：.mutbot/config.json > ~/.mutbot/config.json"""
        monkeypatch.chdir(tmp_path)

        # 模拟 ~/.mutbot/config.json（低优先级）
        mutbot_dir = tmp_path / "fake_home" / ".mutbot"
        mutbot_dir.mkdir(parents=True)
        (mutbot_dir / "config.json").write_text(json.dumps({
            "default_model": "user-model",
            "providers": {"anthropic": {
                "provider": "AnthropicProvider",
                "auth_token": "user-key",
                "models": ["claude-sonnet-4"],
            }},
        }), encoding="utf-8")

        # 模拟 .mutbot/config.json（最高优先级）
        project_dir = tmp_path / ".mutbot"
        project_dir.mkdir()
        (project_dir / "config.json").write_text(json.dumps({
            "default_model": "project-model",
            "providers": {"openai": {
                "provider": "OpenAIProvider",
                "auth_token": "proj-key",
                "models": ["gpt-4.1"],
            }},
        }), encoding="utf-8")

        # 使用显式路径列表（避免依赖 Path.home()）
        config = Config.load([
            str(mutbot_dir / "config.json"),
            str(project_dir / "config.json"),
        ])

        # default_model: 最高优先级 wins
        assert config.get("default_model") == "project-model"
        # providers: 两层合并
        providers = config.get("providers")
        assert "anthropic" in providers
        assert "openai" in providers


# ---------------------------------------------------------------------------
# _write_config() tests (providers format)
# ---------------------------------------------------------------------------

class TestWriteConfig:

    def test_write_creates_new_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mutbot.builtins.setup_provider.MUTBOT_USER_DIR", tmp_path)
        monkeypatch.setattr("mutbot.builtins.setup_provider.MUTBOT_CONFIG_PATH", tmp_path / "config.json")

        _write_config({
            "default_model": "claude-sonnet-4",
            "providers": {
                "anthropic": {
                    "provider": "AnthropicProvider",
                    "auth_token": "sk-test",
                    "models": ["claude-sonnet-4"],
                }
            },
        })

        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["default_model"] == "claude-sonnet-4"
        assert "anthropic" in data["providers"]
        assert data["providers"]["anthropic"]["models"] == ["claude-sonnet-4"]

    def test_write_merges_providers(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mutbot.builtins.setup_provider.MUTBOT_USER_DIR", tmp_path)
        monkeypatch.setattr("mutbot.builtins.setup_provider.MUTBOT_CONFIG_PATH", tmp_path / "config.json")

        # 写入初始配置
        (tmp_path / "config.json").write_text(json.dumps({
            "default_model": "claude-sonnet-4",
            "providers": {
                "anthropic": {
                    "provider": "AnthropicProvider",
                    "models": ["claude-sonnet-4"],
                }
            },
        }), encoding="utf-8")

        # 追加新 provider
        _write_config({
            "default_model": "gpt-4.1",
            "providers": {
                "openai": {
                    "provider": "OpenAIProvider",
                    "models": ["gpt-4.1"],
                }
            },
        })

        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        # default_model 更新为新值
        assert data["default_model"] == "gpt-4.1"
        # providers 合并
        assert "anthropic" in data["providers"]
        assert "openai" in data["providers"]

    def test_write_same_provider_overwrites(self, tmp_path, monkeypatch):
        """同名 provider 覆盖已有的。"""
        monkeypatch.setattr("mutbot.builtins.setup_provider.MUTBOT_USER_DIR", tmp_path)
        monkeypatch.setattr("mutbot.builtins.setup_provider.MUTBOT_CONFIG_PATH", tmp_path / "config.json")

        (tmp_path / "config.json").write_text(json.dumps({
            "providers": {
                "openai": {
                    "provider": "OpenAIProvider",
                    "auth_token": "old-key",
                    "models": ["gpt-4.1"],
                }
            },
        }), encoding="utf-8")

        _write_config({
            "providers": {
                "openai": {
                    "provider": "OpenAIProvider",
                    "auth_token": "new-key",
                    "models": ["gpt-4.1", "gpt-4.1-mini"],
                }
            },
        })

        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["providers"]["openai"]["auth_token"] == "new-key"
        assert len(data["providers"]["openai"]["models"]) == 2


# ---------------------------------------------------------------------------
# Provider auth_token validation tests
# ---------------------------------------------------------------------------

class TestProviderValidation:

    def test_anthropic_requires_auth_token(self):
        from mutagent.builtins.anthropic_provider import AnthropicProvider
        with pytest.raises(ValueError, match="auth_token"):
            AnthropicProvider.from_config({"model_id": "claude-3"})

    def test_anthropic_accepts_auth_token(self):
        from mutagent.builtins.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider.from_config({
            "auth_token": "sk-test-key",
            "base_url": "https://api.anthropic.com",
        })
        assert provider.api_key == "sk-test-key"

    def test_openai_requires_auth_token(self):
        from mutagent.builtins.openai_provider import OpenAIProvider
        with pytest.raises(ValueError, match="auth_token"):
            OpenAIProvider.from_config({"model_id": "gpt-4"})

    def test_openai_accepts_auth_token(self):
        from mutagent.builtins.openai_provider import OpenAIProvider
        provider = OpenAIProvider.from_config({
            "auth_token": "sk-test-key",
            "base_url": "https://api.openai.com/v1",
        })
        assert provider.api_key == "sk-test-key"
