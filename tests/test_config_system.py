"""Tests for mutbot config system (runtime/config.py and cli/setup.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import mutagent.builtins  # noqa: F401  -- register @impl
from mutagent.config import Config
from mutbot.runtime.config import MUTBOT_CONFIG_FILES, load_mutbot_config
from mutbot.cli.setup import _write_config, MUTBOT_CONFIG_PATH


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
            "default_model": "mutbot-model",
            "models": {"from_mutbot": {"model_id": "m2"}},
        }), encoding="utf-8")

        # 模拟 .mutbot/config.json（最高优先级）
        project_dir = tmp_path / ".mutbot"
        project_dir.mkdir()
        (project_dir / "config.json").write_text(json.dumps({
            "default_model": "project-model",
            "models": {"from_project": {"model_id": "m3"}},
        }), encoding="utf-8")

        # 使用显式路径列表（避免依赖 Path.home()）
        config = Config.load([
            str(mutbot_dir / "config.json"),
            str(project_dir / "config.json"),
        ])

        # default_model: 最高优先级 wins
        assert config.get("default_model") == "project-model"
        # models: 两层合并
        models = config.get("models")
        assert "from_mutbot" in models
        assert "from_project" in models


# ---------------------------------------------------------------------------
# _write_config() tests
# ---------------------------------------------------------------------------

class TestWriteConfig:

    def test_write_creates_new_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mutbot.cli.setup.MUTBOT_USER_DIR", tmp_path)
        monkeypatch.setattr("mutbot.cli.setup.MUTBOT_CONFIG_PATH", tmp_path / "config.json")

        _write_config({
            "default_model": "test",
            "models": {"test": {"model_id": "m"}},
        })

        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["default_model"] == "test"
        assert "test" in data["models"]

    def test_write_merges_with_existing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mutbot.cli.setup.MUTBOT_USER_DIR", tmp_path)
        monkeypatch.setattr("mutbot.cli.setup.MUTBOT_CONFIG_PATH", tmp_path / "config.json")

        # 写入初始配置
        (tmp_path / "config.json").write_text(json.dumps({
            "default_model": "existing",
            "models": {"existing": {"model_id": "e"}},
        }), encoding="utf-8")

        # 追加新模型
        _write_config({
            "default_model": "new",
            "models": {"new": {"model_id": "n"}},
        })

        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        # default_model 保留已有的
        assert data["default_model"] == "existing"
        # models 合并
        assert "existing" in data["models"]
        assert "new" in data["models"]


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
