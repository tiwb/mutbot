"""mutbot.auth.login_view — 独立登录页 `/auth/login` 单元测试。

`/auth` 和 `/auth/` 的 302 由 middleware 处理,见 test_public_access_hardening.py。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mutbot.auth.login_view import (
    LoginPageView,
    _safe_next,
)


def _make_request(query: dict[str, str] | None = None) -> MagicMock:
    req = MagicMock()
    req.query_params = query or {}
    return req


class TestSafeNext:
    def test_empty(self) -> None:
        assert _safe_next("") == ""

    def test_relative_path_allowed(self) -> None:
        assert _safe_next("/auth/setup") == "/auth/setup"
        assert _safe_next("/api/health") == "/api/health"
        assert _safe_next("/") == "/"

    def test_protocol_relative_rejected(self) -> None:
        assert _safe_next("//evil.com/x") == ""

    def test_backslash_rejected(self) -> None:
        assert _safe_next("/\\evil.com") == ""

    def test_absolute_url_rejected(self) -> None:
        assert _safe_next("https://evil.com") == ""
        assert _safe_next("javascript:alert(1)") == ""

    def test_no_leading_slash_rejected(self) -> None:
        assert _safe_next("auth/setup") == ""

    def test_newline_rejected(self) -> None:
        assert _safe_next("/auth/setup\nfoo") == ""


class TestLoginPageView:
    @pytest.mark.asyncio
    async def test_get_renders_html(self) -> None:
        view = LoginPageView()
        resp = await view.get(_make_request())
        assert resp.status_code == 200
        assert b"<title>MutBot" in resp.body
        assert b"Sign in to continue" in resp.body
        # JS 引用 /auth/providers
        assert b"/auth/providers" in resp.body

    @pytest.mark.asyncio
    async def test_get_embeds_safe_next_into_js(self) -> None:
        view = LoginPageView()
        resp = await view.get(_make_request(query={"next": "/auth/setup"}))
        assert resp.status_code == 200
        # next 通过 repr() 嵌入 JS 常量 NEXT
        assert b"const NEXT = '/auth/setup'" in resp.body

    @pytest.mark.asyncio
    async def test_get_drops_unsafe_next(self) -> None:
        view = LoginPageView()
        resp = await view.get(_make_request(query={"next": "https://evil.com"}))
        # 不安全的 next 被清空,JS 中 NEXT 为空字符串
        assert b"const NEXT = ''" in resp.body

    @pytest.mark.asyncio
    async def test_get_renders_known_message(self) -> None:
        view = LoginPageView()
        resp = await view.get(_make_request(query={"msg": "session_expired"}))
        assert b"session has expired" in resp.body

    @pytest.mark.asyncio
    async def test_get_drops_unknown_message(self) -> None:
        view = LoginPageView()
        resp = await view.get(_make_request(query={"msg": "<script>alert(1)</script>"}))
        # 未知 msg 静默丢弃,不渲染
        assert b"<script>alert" not in resp.body
