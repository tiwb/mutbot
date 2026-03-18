"""mutbot.auth.token — JWT session token 签发/验证 + relay 断言验证。

Session token: HMAC-SHA256，本地签发，用于 HTTP cookie 和 WebSocket 认证。
Relay 断言: Ed25519，由中转站签发，本地验证公钥。
"""

from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from typing import Any

import jwt

from mutbot.runtime.config import MUTBOT_USER_DIR

logger = logging.getLogger(__name__)

_AUTH_SECRET_PATH = MUTBOT_USER_DIR / "auth_secret"

# ---------------------------------------------------------------------------
# Session token（HMAC-SHA256）
# ---------------------------------------------------------------------------


def _load_or_create_secret() -> str:
    """加载或自动生成 session 签名密钥。持久化到 ~/.mutbot/auth_secret。"""
    if _AUTH_SECRET_PATH.exists():
        secret = _AUTH_SECRET_PATH.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    secret = secrets.token_hex(32)
    _AUTH_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    import os, stat
    # 写入并限制权限为仅 owner 可读写
    fd = os.open(str(_AUTH_SECRET_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(secret)
    logger.info("生成新的 auth secret: %s", _AUTH_SECRET_PATH)
    return secret


_session_secret: str | None = None


def _get_session_secret() -> str:
    global _session_secret
    if _session_secret is None:
        _session_secret = _load_or_create_secret()
    return _session_secret


def create_session_token(
    *,
    sub: str,
    name: str,
    avatar: str = "",
    provider: str,
    ttl: int = 604800,
) -> str:
    """签发 session JWT。

    Args:
        sub: 用户标识，格式 provider:username
        name: 显示名
        avatar: 头像 URL
        provider: 认证来源
        ttl: 有效期（秒），默认 7 天
    """
    now = int(time.time())
    payload = {
        "sub": sub,
        "name": name,
        "avatar": avatar,
        "provider": provider,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, _get_session_secret(), algorithm="HS256")


def verify_session_token(token: str) -> dict[str, Any] | None:
    """验证 session JWT。返回 payload dict 或 None（无效/过期）。"""
    try:
        return jwt.decode(token, _get_session_secret(), algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None


# ---------------------------------------------------------------------------
# Relay 断言验证（Ed25519）
# ---------------------------------------------------------------------------


def verify_relay_assertion(token: str, public_key_pem: str) -> dict[str, Any] | None:
    """验证中转站签发的断言 JWT（Ed25519 / EdDSA）。

    Args:
        token: 断言 JWT 字符串
        public_key_pem: Ed25519 公钥（PEM 格式）

    Returns:
        验证通过返回 payload dict，否则返回 None。
    """
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        key = load_pem_public_key(public_key_pem.encode("utf-8"))
        # audience 由调用方单独校验，此处跳过
        return jwt.decode(token, key, algorithms=["EdDSA"], options={"verify_aud": False})
    except Exception as e:
        logger.debug("relay 断言验证失败: %s", e)
        return None


# ---------------------------------------------------------------------------
# Cookie 辅助
# ---------------------------------------------------------------------------

COOKIE_NAME = "mutbot_token"


def set_session_cookie(headers: dict[str, str], token: str, *, secure: bool = False) -> None:
    """设置 session cookie 到响应 headers。"""
    parts = [
        f"{COOKIE_NAME}={token}",
        "Path=/",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    headers["set-cookie"] = "; ".join(parts)


def clear_session_cookie(headers: dict[str, str], *, secure: bool = False) -> None:
    """清除 session cookie。"""
    parts = [
        f"{COOKIE_NAME}=",
        "Path=/",
        "SameSite=Lax",
        "Max-Age=0",
    ]
    if secure:
        parts.append("Secure")
    headers["set-cookie"] = "; ".join(parts)


def extract_token_from_cookie(cookie_header: str) -> str | None:
    """从 Cookie header 中提取 mutbot_token。"""
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{COOKIE_NAME}="):
            value = part[len(COOKIE_NAME) + 1:]
            return value if value else None
    return None
