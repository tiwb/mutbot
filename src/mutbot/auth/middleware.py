"""mutbot.auth.middleware — before_route @impl，认证拦截逻辑。

此模块需要被 import 以注册 @impl。在 server.py 的 on_startup 中 import。
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

import mutobj
from mutagent.net.server import Server, Response

from mutbot.auth.token import verify_session_token, extract_token_from_cookie
from mutbot.auth.network import resolve_client_ip, is_loopback_ip

logger = logging.getLogger(__name__)

# 当前请求的解析后客户端 IP（供 View 使用）
current_client_ip: contextvars.ContextVar[str] = contextvars.ContextVar("current_client_ip", default="")

# 白名单路径前缀 — 不需要认证
_PUBLIC_PREFIXES = (
    "/auth/",
    "/.well-known/",
    "/api/health",
)

# 仅允许本地访问的路径（不需要认证，但限制来源 IP）
_LOCAL_ONLY_PREFIXES = (
    "/mcp",
    "/internal/",
)


def _is_public_path(path: str) -> bool:
    """检查路径是否在白名单中（不需要认证）。"""
    for prefix in _PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _is_static_path(path: str) -> bool:
    """检查是否为静态资源请求。"""
    # 常见静态资源扩展名
    static_exts = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".map")
    return any(path.endswith(ext) for ext in static_exts)


def _get_auth_config() -> dict[str, Any] | None:
    """获取 auth 配置。"""
    from mutbot.web import server as _server_mod
    cfg = _server_mod.config
    if cfg is None:
        return None
    return cfg.get("auth")


def _get_trusted_proxies() -> list[str]:
    """获取 trusted_proxies 配置。"""
    from mutbot.web import server as _server_mod
    cfg = _server_mod.config
    if cfg is None:
        return ["127.0.0.1", "::1"]
    return cfg.get("security.trusted_proxies", default=["127.0.0.1", "::1"]) or ["127.0.0.1", "::1"]


def _extract_user_from_scope(scope: dict[str, Any]) -> dict[str, Any] | None:
    """从 ASGI scope（HTTP 或 WebSocket）的 headers 中提取并验证 session token。"""
    raw_headers = scope.get("headers", [])
    cookie_header = ""
    for k, v in raw_headers:
        if k == b"cookie":
            cookie_header = v.decode("latin-1")
            break

    if not cookie_header:
        # WebSocket: 也检查 query param token
        qs = scope.get("query_string", b"")
        if isinstance(qs, bytes):
            qs = qs.decode("latin-1")
        if "token=" in qs:
            from urllib.parse import parse_qs
            params = parse_qs(qs)
            token_list = params.get("token", [])
            if token_list:
                return verify_session_token(token_list[0])
        return None

    token = extract_token_from_cookie(cookie_header)
    if not token:
        return None
    return verify_session_token(token)


@mutobj.impl(Server.before_route)
async def _mutbot_before_route(self: Server, scope: dict[str, Any], path: str) -> Response | None:
    """mutbot 认证拦截。

    无 auth 配置 + 本地请求 → 放行（None）
    无 auth 配置 + 非本地请求 → 重定向到 /auth/setup
    白名单路径 → 放行
    已认证 → 放行（用户信息注入 scope）
    未认证 HTTP → 302 到 /
    未认证 WebSocket → Response(status=4401) → _server_impl 转为 ws.close
    """
    auth_config = _get_auth_config()
    trusted_proxies = _get_trusted_proxies()
    client_ip = resolve_client_ip(scope, trusted_proxies)

    # 注入解析后的 client IP（供 View 通过 contextvars 访问 + access log）
    current_client_ip.set(client_ip)
    scope["real_client_ip"] = client_ip

    # 无 auth 配置
    if not auth_config or (not auth_config.get("relay") and not auth_config.get("providers")):
        # 本地请求 → 放行（行为不变）
        if is_loopback_ip(client_ip):
            logger.debug("allow local (no auth): %s %s", client_ip, path)
            return None

        # 非本地请求 → 拦截，重定向到 /auth/setup
        # 白名单路径放行（/auth/ 前缀包含 /auth/setup）
        if _is_public_path(path):
            return None

        # 其余非本地请求（含根路径）→ 重定向到 /auth/setup
        base_path = ""
        from mutbot.web import server as _server_mod
        if _server_mod.config is not None:
            base_path = _server_mod.config.get("base_path", default="") or ""

        scope_type = scope.get("type")
        if scope_type == "websocket":
            logger.info("reject ws (no auth): %s %s", client_ip, path)
            return Response(status=4401)
        logger.info("redirect to /auth/setup (no auth): %s %s", client_ip, path)
        return Response(
            status=302,
            headers={"location": base_path + "/auth/setup"},
        )

    # 白名单路径 → 放行
    if _is_public_path(path):
        return None

    # 仅本地访问路径 → 检查来源 IP（使用 resolve_client_ip）
    for prefix in _LOCAL_ONLY_PREFIXES:
        if path.startswith(prefix):
            if is_loopback_ip(client_ip):
                return None
            logger.warning("deny local-only path: %s %s", client_ip, path)
            return Response(status=403)

    # 静态资源 → 放行（登录页面需要加载 CSS/JS）
    if _is_static_path(path):
        return None

    # 根路径 → 放行（让 React App 加载，前端自行判断登录状态）
    if path == "/" or path == "":
        return None

    # 尝试提取用户身份
    user = _extract_user_from_scope(scope)
    if user:
        # 认证通过，注入用户信息到 scope
        scope["user"] = user
        logger.debug("allow authenticated: %s %s", user.get("sub", "?"), path)
        return None

    # 未认证
    scope_type = scope.get("type")
    if scope_type == "websocket":
        logger.info("reject ws (unauthenticated): %s %s", client_ip, path)
        return Response(status=4401)

    # HTTP: 重定向到登录页
    base_path = ""
    from mutbot.web import server as _server_mod
    if _server_mod.config is not None:
        base_path = _server_mod.config.get("base_path", default="") or ""
    logger.info("redirect to login: %s %s", client_ip, path)
    return Response(
        status=302,
        headers={"location": base_path + "/"},
    )
