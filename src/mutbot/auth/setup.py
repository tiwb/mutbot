"""mutbot.auth.setup — Auth 配置 setup nonce 管理与配置保存。

HTTP setup 流程（/auth/setup）使用临时 nonce 关联 OAuth 回调。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 临时 setup nonce 状态（内存中，5 分钟 TTL）
# ---------------------------------------------------------------------------

_pending_setup: dict[str, dict[str, Any]] = {}
_SETUP_NONCE_TTL = 300  # 5 分钟


def store_setup_nonce(nonce: str, relay_url: str, access_mode: str) -> None:
    """存储向导收集的信息，绑定到 nonce。"""
    _cleanup_expired()
    _pending_setup[nonce] = {
        "relay_url": relay_url,
        "access_mode": access_mode,
        "created": time.time(),
    }


def pop_setup_nonce(nonce: str) -> dict[str, Any] | None:
    """消费一个 setup nonce。找到则返回信息并删除，否则返回 None。"""
    _cleanup_expired()
    return _pending_setup.pop(nonce, None)


def _cleanup_expired() -> None:
    now = time.time()
    expired = [k for k, v in _pending_setup.items() if now - v["created"] > _SETUP_NONCE_TTL]
    for k in expired:
        del _pending_setup[k]


def save_auth_config(relay_url: str, access_mode: str, user_sub: str) -> None:
    """保存 auth 配置到 ~/.mutbot/config.json（合并，保留已有的 relay_service 等）。"""
    from mutbot.web import server as _server_mod
    config = _server_mod.config
    if config is None:
        return

    # 用点分路径写入，不覆盖 auth 下的其他 key（如 relay_service）
    config.set("auth.relay", relay_url)
    config.set("auth.allowed_users", [user_sub])

    # auth 配置完成 → 使 setup token 失效
    import mutbot.auth.setup_token as _setup_token
    _setup_token.invalidate()

    logger.info("Auth config saved: relay=%s, user=%s", relay_url, user_sub)
