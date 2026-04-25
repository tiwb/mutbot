"""mutbot.auth.views.ProvidersView — /auth/providers JSON 响应单元测试。

聚焦 setup-token 选项的拼装逻辑(本次重构新增)。
"""
from __future__ import annotations

import json
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

import mutbot.web.server as _server_mod
import mutbot.auth.setup_token as setup_token

from mutbot.auth.views import ProvidersView


class _FakeConfig:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = data or {}

    def get(self, name: str, *, default: Any = None) -> Any:
        node = self._data
        for k in name.split("."):
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def set(self, name: str, value: Any, *, source: str = "") -> None:
        node = self._data
        keys = name.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value


@pytest.fixture(autouse=True)
def _reset_token() -> Iterator[None]:
    setup_token.invalidate()
    yield
    setup_token.invalidate()


def _make_request() -> MagicMock:
    req = MagicMock()
    req.headers = {"host": "localhost:8741"}
    return req


async def _call(view: ProvidersView) -> dict[str, Any]:
    resp = await view.get(_make_request())
    return json.loads(resp.body)


@pytest.fixture
def fake_unconfigured(monkeypatch: pytest.MonkeyPatch) -> _FakeConfig:
    cfg = _FakeConfig({})
    monkeypatch.setattr(_server_mod, "config", cfg)
    return cfg


@pytest.fixture
def fake_configured_relay(monkeypatch: pytest.MonkeyPatch) -> _FakeConfig:
    """已配置 relay auth(httpx mock 由各 test 自行处理)。"""
    cfg = _FakeConfig({"auth": {"relay": "https://mutbot.ai", "allowed_users": ["u1"]}})
    monkeypatch.setattr(_server_mod, "config", cfg)
    return cfg


class TestSetupTokenOption:
    @pytest.mark.asyncio
    async def test_no_auth_no_token_empty(self, fake_unconfigured: _FakeConfig) -> None:
        data = await _call(ProvidersView())
        assert data["auth_enabled"] is False
        assert data["providers"] == []

    @pytest.mark.asyncio
    async def test_no_auth_with_token_includes_setup_option(self, fake_unconfigured: _FakeConfig) -> None:
        setup_token.generate()
        data = await _call(ProvidersView())
        assert data["auth_enabled"] is False
        names = [p["name"] for p in data["providers"]]
        assert "setup-token" in names
        setup_opt = next(p for p in data["providers"] if p["name"] == "setup-token")
        assert setup_opt["type"] == "setup-token"
        assert setup_opt["url"] == "/auth/setup-token-login"
        assert setup_opt["label"] == "Setup Token"

    @pytest.mark.asyncio
    async def test_token_invalidated_drops_setup_option(self, fake_unconfigured: _FakeConfig) -> None:
        setup_token.generate()
        setup_token.invalidate()
        data = await _call(ProvidersView())
        assert all(p["name"] != "setup-token" for p in data["providers"])

    @pytest.mark.asyncio
    async def test_configured_with_token_setup_option_first(
        self, fake_configured_relay: _FakeConfig, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """已配置 + 有活跃 token → setup-token 选项排在最前面。

        管理员锁出自己后通过重启 / CLI 重新生成 token 恢复。
        """
        setup_token.generate()

        async def _fake_fetch(_url: str) -> list[str]:
            return ["github"]
        import mutbot.auth.views as _views_mod
        monkeypatch.setattr(_views_mod, "_fetch_relay_providers", _fake_fetch)

        data = await _call(ProvidersView())
        assert data["auth_enabled"] is True
        names = [p["name"] for p in data["providers"]]
        assert names[0] == "setup-token", f"setup-token 应在最前面,实际: {names}"
        assert "github" in names  # relay provider 仍然存在
