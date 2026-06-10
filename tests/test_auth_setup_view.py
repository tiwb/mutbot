"""mutbot.auth.setup_view — AuthSetupView (mutgui) 状态机单元测试。

不启动 WebSocket,纯 Python 测试 View 的状态转换。
mock 关键外部依赖:mutbot.web.server.config / _fetch_relay_providers。

鉴权由 middleware 在连接级处理(见 test_public_access_hardening.py),
本 View 不做任何鉴权检查 — 进来即认为已通过。
"""
from __future__ import annotations

from typing import Any

import pytest

import mutbot.web.server as _server_mod
import mutbot.auth.views as _auth_views

from mutbot.auth.setup_view import AuthSetupView


# ---------------------------------------------------------------------------
# Fake config
# ---------------------------------------------------------------------------


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


@pytest.fixture
def fake_unconfigured(monkeypatch: pytest.MonkeyPatch) -> _FakeConfig:
    cfg = _FakeConfig({})
    monkeypatch.setattr(_server_mod, "config", cfg)
    return cfg


@pytest.fixture
def fake_configured(monkeypatch: pytest.MonkeyPatch) -> _FakeConfig:
    cfg = _FakeConfig({"auth": {"relay": "https://mutbot.ai", "allowed_users": ["u1"]}})
    monkeypatch.setattr(_server_mod, "config", cfg)
    return cfg


# ---------------------------------------------------------------------------
# 初始 step 由是否已配置决定
# ---------------------------------------------------------------------------


class TestInitialStep:
    def test_unconfigured_starts_at_configure(self, fake_unconfigured: _FakeConfig) -> None:
        v = AuthSetupView()
        assert v.step == "configure"

    def test_already_configured_starts_at_already_configured(self, fake_configured: _FakeConfig) -> None:
        v = AuthSetupView()
        assert v.step == "already_configured"


# ---------------------------------------------------------------------------
# Connect relay → fetch providers
# ---------------------------------------------------------------------------


class TestConnectRelay:
    @pytest.mark.asyncio
    async def test_connect_success(self, fake_unconfigured: _FakeConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_fetch(_url: str) -> list[str]:
            return ["github", "google"]
        monkeypatch.setattr(_auth_views, "_fetch_relay_providers", _fake_fetch)

        v = AuthSetupView()
        v.relay_url = "https://relay.example.com"
        await v._on_connect_relay()
        assert v.step == "select_provider"
        assert [p["name"] for p in v.providers] == ["github", "google"]

    @pytest.mark.asyncio
    async def test_relay_unreachable_sets_error(self, fake_unconfigured: _FakeConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_fetch(_url: str) -> list[str]:
            return []
        monkeypatch.setattr(_auth_views, "_fetch_relay_providers", _fake_fetch)

        v = AuthSetupView()
        v.relay_url = "https://relay.example.com"
        await v._on_connect_relay()
        assert v.step == "configure"
        assert "Cannot connect" in v.error

    @pytest.mark.asyncio
    async def test_ssrf_blocked(self, fake_unconfigured: _FakeConfig) -> None:
        v = AuthSetupView()
        v.relay_url = "http://192.168.1.1"
        await v._on_connect_relay()
        assert v.step == "configure"
        assert v.error  # SSRF error 文案

    @pytest.mark.asyncio
    async def test_empty_relay_url_sets_error(self, fake_unconfigured: _FakeConfig) -> None:
        v = AuthSetupView()
        v.relay_url = "  "
        await v._on_connect_relay()
        assert v.step == "configure"
        assert "Please enter" in v.error


# ---------------------------------------------------------------------------
# OAuth 跳转
# ---------------------------------------------------------------------------


class TestStartOAuth:
    @pytest.mark.asyncio
    async def test_start_oauth_sends_redirect_command(
        self,
        fake_unconfigured: _FakeConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        v = AuthSetupView()
        v.relay_url = "https://relay.example.com"
        v._ws_host = "localhost:8741"
        sent: list[tuple[str, dict[str, Any]]] = []

        async def _fake_send_command(name: str, /, **args: Any) -> None:
            sent.append((name, args))

        v._mock_send_command = _fake_send_command
        await v._on_start_oauth("github")
        assert sent
        name, args = sent[0]
        assert name == "mutgui.redirect"
        url = args["url"]
        assert url.startswith("https://relay.example.com/auth/start")
        assert "provider=github" in url
        assert "callback=" in url
        assert "nonce=" in url

    @pytest.mark.asyncio
    async def test_empty_provider_sets_error(self, fake_unconfigured: _FakeConfig) -> None:
        v = AuthSetupView()
        await v._on_start_oauth("")
        assert v.step == "configure"
        assert v.error


# ---------------------------------------------------------------------------
# Reconfigure 流程 — 简化为单分支(鉴权由 middleware 处理)
# ---------------------------------------------------------------------------


class TestReconfigure:
    def test_reconfigure_advances_to_configure(self, fake_configured: _FakeConfig) -> None:
        v = AuthSetupView()
        assert v.step == "already_configured"
        v._on_reconfigure()
        assert v.step == "configure"
        # 不再清空 config — 保持旧配置直到 OAuth 成功覆盖
        assert fake_configured.get("auth.relay") == "https://mutbot.ai"
        # relay_url 预填当前值
        assert v.relay_url == "https://mutbot.ai"


# ---------------------------------------------------------------------------
# 后退导航
# ---------------------------------------------------------------------------


class TestBackNavigation:
    def test_back_to_configure(self, fake_unconfigured: _FakeConfig) -> None:
        v = AuthSetupView()
        v.step = "select_provider"
        v.error = "stale"
        v._on_back_to_configure()
        assert v.step == "configure"
        assert v.error == ""

    @pytest.mark.asyncio
    async def test_back_home_triggers_redirect(
        self,
        fake_configured: _FakeConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        v = AuthSetupView()
        sent: list[tuple[str, dict[str, Any]]] = []

        async def _fake_send_command(name: str, /, **args: Any) -> None:
            sent.append((name, args))

        v._mock_send_command = _fake_send_command
        await v._on_back_home()
        assert sent == [("mutgui.redirect", {"url": "/"})]

    def test_back_to_configured_from_configure(self, fake_configured: _FakeConfig) -> None:
        v = AuthSetupView()
        v._on_reconfigure()
        assert v.step == "configure"
        v._on_back_to_configured()
        assert v.step == "already_configured"
