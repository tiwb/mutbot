"""mutbot.auth.setup_login — Setup token 登录路由单元测试。"""
from __future__ import annotations

from typing import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

import mutbot.auth.setup_token as setup_token
from mutbot.auth.setup_login import (
    SETUP_BOOTSTRAP_SUB,
    SETUP_SESSION_TTL,
    SetupTokenLoginView,
)
from mutbot.auth.token import COOKIE_NAME, verify_session_token


@pytest.fixture(autouse=True)
def _reset_token() -> Iterator[None]:
    setup_token.invalidate()
    yield
    setup_token.invalidate()


def _make_request(
    *, body: bytes = b"", headers: dict[str, str] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.headers = headers or {}
    req.body = AsyncMock(return_value=body)
    return req


class TestGet:
    @pytest.mark.asyncio
    async def test_get_returns_form_when_token_active(self) -> None:
        setup_token.generate()
        view = SetupTokenLoginView()
        resp = await view.get(_make_request())
        assert resp.status == 200
        assert b"Setup Token Required" in resp.body
        assert b'name="token"' in resp.body

    @pytest.mark.asyncio
    async def test_get_redirects_when_token_inactive(self) -> None:
        view = SetupTokenLoginView()
        resp = await view.get(_make_request())
        assert resp.status == 302
        assert resp.headers.get("location") == "/auth/login"


class TestPost:
    @pytest.mark.asyncio
    async def test_post_redirects_when_token_inactive(self) -> None:
        view = SetupTokenLoginView()
        resp = await view.post(_make_request(body=b"token=anything"))
        assert resp.status == 302
        assert resp.headers.get("location") == "/auth/login"

    @pytest.mark.asyncio
    async def test_post_empty_token_400(self) -> None:
        setup_token.generate()
        view = SetupTokenLoginView()
        resp = await view.post(_make_request(body=b"token="))
        assert resp.status == 400
        assert b"Please enter" in resp.body

    @pytest.mark.asyncio
    async def test_post_invalid_token_401(self) -> None:
        setup_token.generate()
        view = SetupTokenLoginView()
        resp = await view.post(_make_request(body=b"token=wrong-token"))
        assert resp.status == 401
        assert b"Invalid token" in resp.body

    @pytest.mark.asyncio
    async def test_post_correct_token_issues_session(self) -> None:
        token = setup_token.generate()
        view = SetupTokenLoginView()
        body = f"token={token}".encode("utf-8")
        resp = await view.post(_make_request(body=body))

        assert resp.status == 302
        assert resp.headers.get("location") == "/auth/setup"
        cookie = resp.headers.get("set-cookie", "")
        assert COOKIE_NAME in cookie
        # 提取 session token 验证 payload
        session_value = cookie.split(f"{COOKIE_NAME}=", 1)[1].split(";", 1)[0]
        payload = verify_session_token(session_value)
        assert payload is not None
        assert payload["sub"] == SETUP_BOOTSTRAP_SUB
        assert payload["provider"] == "setup-token"
        assert payload["exp"] - payload["iat"] == SETUP_SESSION_TTL

    @pytest.mark.asyncio
    async def test_post_secure_flag_when_https(self) -> None:
        token = setup_token.generate()
        view = SetupTokenLoginView()
        body = f"token={token}".encode("utf-8")
        resp = await view.post(_make_request(
            body=body,
            headers={"x-forwarded-proto": "https"},
        ))
        assert "Secure" in resp.headers.get("set-cookie", "")

    @pytest.mark.asyncio
    async def test_post_url_encoded_token_decoded(self) -> None:
        token = setup_token.generate()
        view = SetupTokenLoginView()
        # 模拟 URL encoding(token 含 -,通常不需要 encoding,但确保 parse_qsl 正常)
        body = f"token={token}&extra=ignored".encode("utf-8")
        resp = await view.post(_make_request(body=body))
        assert resp.status == 302
