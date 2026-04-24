"""mutbot.auth.setup_view — AuthSetupView (mutgui) 状态机单元测试。

不启动 WebSocket，纯 Python 测试 View 的状态转换。
mock 关键外部依赖：mutbot.web.server.config / setup_token / _fetch_relay_providers。
"""
from __future__ import annotations

from typing import Any, Iterator

import pytest

import mutbot.web.server as _server_mod
import mutbot.auth.setup_token as setup_token
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


@pytest.fixture(autouse=True)
def _reset_setup_token() -> Iterator[None]:
    setup_token.invalidate()
    yield
    setup_token.invalidate()


# ---------------------------------------------------------------------------
# 初始 step 由 is_local + setup_token + 已配置状态决定
# ---------------------------------------------------------------------------


class TestInitialStep:
    def test_local_unconfigured_starts_at_configure(self, fake_unconfigured: _FakeConfig) -> None:
        v = AuthSetupView(is_local=True)
        assert v.step == "configure"

    def test_remote_unconfigured_no_token_starts_at_configure(self, fake_unconfigured: _FakeConfig) -> None:
        v = AuthSetupView(is_local=False)
        assert v.step == "configure"

    def test_remote_unconfigured_with_token_starts_at_token_input(self, fake_unconfigured: _FakeConfig) -> None:
        setup_token.generate()
        v = AuthSetupView(is_local=False)
        assert v.step == "token_input"

    def test_already_configured_starts_at_already_configured(self, fake_configured: _FakeConfig) -> None:
        v = AuthSetupView(is_local=False)
        assert v.step == "already_configured"


# ---------------------------------------------------------------------------
# Token 验证
# ---------------------------------------------------------------------------


class TestVerifyToken:
    @pytest.mark.asyncio
    async def test_empty_token_sets_error(self, fake_unconfigured: _FakeConfig) -> None:
        setup_token.generate()
        v = AuthSetupView(is_local=False)
        v.token_input = "   "
        await v._on_verify_token()
        assert v.step == "token_input"
        assert "enter" in v.error.lower()

    @pytest.mark.asyncio
    async def test_wrong_token_sets_error(self, fake_unconfigured: _FakeConfig) -> None:
        setup_token.generate()
        v = AuthSetupView(is_local=False)
        v.token_input = "not-the-token"
        await v._on_verify_token()
        assert v.step == "token_input"
        assert "invalid" in v.error.lower()

    @pytest.mark.asyncio
    async def test_correct_token_advances_to_configure(self, fake_unconfigured: _FakeConfig) -> None:
        token = setup_token.generate()
        v = AuthSetupView(is_local=False)
        v.token_input = token
        await v._on_verify_token()
        assert v.step == "configure"
        assert v.setup_verified is True
        assert v.error == ""
        assert v.token_input == ""


# ---------------------------------------------------------------------------
# Connect relay → fetch providers
# ---------------------------------------------------------------------------


class TestConnectRelay:
    @pytest.mark.asyncio
    async def test_local_connect_success(self, fake_unconfigured: _FakeConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_fetch(_url: str) -> list[str]:
            return ["github", "google"]
        monkeypatch.setattr(_auth_views, "_fetch_relay_providers", _fake_fetch)

        v = AuthSetupView(is_local=True)
        v.relay_url = "https://relay.example.com"
        await v._on_connect_relay()
        assert v.step == "select_provider"
        assert [p["name"] for p in v.providers] == ["github", "google"]

    @pytest.mark.asyncio
    async def test_relay_unreachable_sets_error(self, fake_unconfigured: _FakeConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_fetch(_url: str) -> list[str]:
            return []
        monkeypatch.setattr(_auth_views, "_fetch_relay_providers", _fake_fetch)

        v = AuthSetupView(is_local=True)
        v.relay_url = "https://relay.example.com"
        await v._on_connect_relay()
        assert v.step == "configure"
        assert "Cannot connect" in v.error

    @pytest.mark.asyncio
    async def test_ssrf_blocked(self, fake_unconfigured: _FakeConfig) -> None:
        v = AuthSetupView(is_local=True)
        v.relay_url = "http://192.168.1.1"
        await v._on_connect_relay()
        assert v.step == "configure"
        assert v.error  # SSRF error 文案

    @pytest.mark.asyncio
    async def test_remote_unverified_with_token_active_falls_back(
        self, fake_unconfigured: _FakeConfig, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        setup_token.generate()
        v = AuthSetupView(is_local=False)
        # 跳过初始 token_input：手动放到 configure 但未 verified
        v.step = "configure"
        v.setup_verified = False
        v.relay_url = "https://relay.example.com"
        await v._on_connect_relay()
        assert v.step == "token_input"


# ---------------------------------------------------------------------------
# OAuth 跳转
# ---------------------------------------------------------------------------


class TestStartOAuth:
    @pytest.mark.asyncio
    async def test_start_oauth_sets_redirect(self, fake_unconfigured: _FakeConfig) -> None:
        v = AuthSetupView(is_local=True)
        v.relay_url = "https://relay.example.com"
        v._ws_host = "localhost:8741"
        await v._on_start_oauth("github")
        assert v.step == "redirecting"
        assert v.redirect_url.startswith("https://relay.example.com/auth/start")
        assert "provider=github" in v.redirect_url
        assert "callback=" in v.redirect_url
        assert "nonce=" in v.redirect_url


# ---------------------------------------------------------------------------
# Reconfigure 流程
# ---------------------------------------------------------------------------


class TestReconfigure:
    @pytest.mark.asyncio
    async def test_local_reconfigure_clears_and_advances(self, fake_configured: _FakeConfig) -> None:
        v = AuthSetupView(is_local=True)
        assert v.step == "already_configured"
        await v._on_reconfigure()
        assert v.step == "configure"
        # config.auth.relay 被清空
        assert fake_configured.get("auth.relay") is None

    @pytest.mark.asyncio
    async def test_remote_reconfigure_requires_token(self, fake_configured: _FakeConfig, capsys: pytest.CaptureFixture[str]) -> None:
        v = AuthSetupView(is_local=False)
        assert v.step == "already_configured"
        await v._on_reconfigure()
        assert v.step == "token_input"
        assert v.pending_reconfigure is True
        assert setup_token.is_active() is True
        out = capsys.readouterr().out
        assert "Setup Token" in out

    @pytest.mark.asyncio
    async def test_remote_reconfigure_after_token_clears_config(self, fake_configured: _FakeConfig) -> None:
        v = AuthSetupView(is_local=False)
        await v._on_reconfigure()
        token = setup_token._token  # 拿当前生成的 token
        assert token is not None
        v.token_input = token
        await v._on_verify_token()
        assert v.step == "configure"
        assert v.pending_reconfigure is False
        assert fake_configured.get("auth.relay") is None
