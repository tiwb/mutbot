"""mutbot.auth.middleware — before_route @impl，认证拦截逻辑。

此模块需要被 import 以注册 @impl。在 server.py 的 on_startup 中 import。
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

import mutobj
from mutio.net.server import RedirectResponse, Response, Server

from mutbot.auth.token import verify_session_token, extract_token_from_cookie
from mutbot.auth.network import resolve_client_ip, is_loopback_ip

logger = logging.getLogger(__name__)

# 当前请求的解析后客户端 IP（供 View 使用）
current_client_ip: contextvars.ContextVar[str] = contextvars.ContextVar("current_client_ip", default="")

# 白名单路径前缀 — 不需要认证
# 注:`/auth/setup` 和 `/auth/setup/ws` 故意不在内 — setup 是 root 级操作,必须经过登录
# (setup token 通过 /auth/setup-token-login 验证,签发标准 session)
_PUBLIC_PREFIXES = (
    "/auth/callback",
    "/auth/relay-callback",
    "/auth/providers",
    "/auth/userinfo",
    "/auth/logout",
    "/auth/setup-token-login",
    "/auth/login",  # 独立登录页(纯 HTML)
    "/.well-known/",
    "/api/health",
)

# setup-token session(sub == SETUP_BOOTSTRAP_SUB)允许访问的路径
# 该身份"能且仅能"做 setup 相关操作,其他业务路径 403
_SETUP_ALLOWED_PATHS = (
    "/auth/setup",
    "/auth/setup/ws",
    "/auth/relay-callback",
)

# 仅允许本地访问的路径（不需要认证，但限制来源 IP）
_LOCAL_ONLY_PREFIXES = (
    "/mcp",
    "/internal/",
    "/llm",
)


def _is_public_path(path: str) -> bool:
    """检查路径是否在白名单中（不需要认证）。"""
    for prefix in _PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _login_redirect_target(base_path: str, original_path: str) -> str:
    """构造未登录时的重定向 URL，把原路径塞到 next 参数。

    根路径不传 next（登录后默认就回 /）。
    """
    from urllib.parse import quote
    if original_path == "/" or not original_path:
        return base_path + "/auth/login"
    return base_path + "/auth/login?next=" + quote(original_path, safe="/")


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

    无 auth 配置 + 本地请求 → 放行(setup 路径除外)
    无 auth 配置 + 非本地请求 + 业务路径(含 /) → 重定向到 /auth/login(独立登录页)
    白名单路径 → 放行
    setup-token session(sub=setup:bootstrap)→ 仅放行 setup 相关路径,/ 跳 setup,其他 403
    已认证 → 放行(用户信息注入 scope)
    未认证 HTTP(含 /) → 302 到 /auth/login?next=<原路径>
    未认证 WebSocket → Response(status_code=4401) → _server_impl 转为 ws.close
    """
    from mutbot.auth.setup_login import SETUP_BOOTSTRAP_SUB
    # import 触发 LoginPageView 注册
    import mutbot.auth.login_view as _login_view  # noqa: F401

    auth_config = _get_auth_config()
    trusted_proxies = _get_trusted_proxies()
    client_ip = resolve_client_ip(scope, trusted_proxies)

    # 注入解析后的 client IP(供 View 通过 contextvars 访问 + access log)
    current_client_ip.set(client_ip)
    scope["real_client_ip"] = client_ip

    base_path = ""
    from mutbot.web import server as _server_mod
    if _server_mod.config is not None:
        base_path = _server_mod.config.get("base_path", default="") or ""

    # `/auth` 和 `/auth/` → 直接 302 到 `/auth/login`(URL 规范化,无视登录态)
    # 早于其他逻辑处理,避免 trailing slash 漏到下方分支返回 404/空页
    if path == "/auth" or path == "/auth/":
        next_param = ""
        qs = scope.get("query_string", b"")
        if isinstance(qs, bytes):
            qs = qs.decode("latin-1")
        if "next=" in qs:
            from urllib.parse import parse_qs
            params = parse_qs(qs)
            cand = params.get("next", [""])[0]
            if cand.startswith("/") and not cand.startswith("//") and not cand.startswith("/\\") \
                    and "\n" not in cand and "\r" not in cand:
                next_param = cand
        from urllib.parse import quote
        target = base_path + "/auth/login"
        if next_param:
            target = target + "?next=" + quote(next_param, safe="/")
        return RedirectResponse(target, status_code=302)

    # setup 路径需要登录 — 即使本地访问也不再特殊放行
    # 这是"setup 是 root 级操作,一律鉴权"的落地点
    is_setup_path = path == "/auth/setup" or path == "/auth/setup/ws"

    # 无 auth 配置
    if not auth_config or (not auth_config.get("relay") and not auth_config.get("providers")):
        # 本地请求 + 非 setup 路径 → 放行(行为不变)
        if is_loopback_ip(client_ip) and not is_setup_path:
            logger.debug("allow local (no auth): %s %s", client_ip, path)
            return None

        # 白名单路径放行(包含 /auth/login、/auth/setup-token-login、relay-callback 等)
        if _is_public_path(path):
            return None

        # setup 路径:即使本地也走标准登录(确保 setup-token session 生效后才能进)
        if is_setup_path:
            user = _extract_user_from_scope(scope)
            if user and user.get("sub") == SETUP_BOOTSTRAP_SUB:
                scope["user"] = user
                return None
            scope_type = scope.get("type")
            if scope_type == "websocket":
                logger.info("reject ws (setup needs login): %s %s", client_ip, path)
                return Response(status_code=4401)
            target = _login_redirect_target(base_path, path)
            logger.info("redirect to login (setup needs login): %s → %s", client_ip, target)
            return RedirectResponse(target, status_code=302)

        # 静态资源放行(/auth/login 是纯 HTML 不依赖,但 React App 加载需要)
        if _is_static_path(path):
            return None

        # 其他业务路径(含根路径)→ 跳 /auth/login
        scope_type = scope.get("type")
        if scope_type == "websocket":
            logger.info("reject ws (no auth): %s %s", client_ip, path)
            return Response(status_code=4401)
        target = _login_redirect_target(base_path, path)
        logger.info("redirect to login (no auth): %s → %s", client_ip, target)
        return RedirectResponse(target, status_code=302)

    # 白名单路径 → 放行
    if _is_public_path(path):
        return None

    # 仅本地访问路径 → 检查来源 IP(使用 resolve_client_ip)
    for prefix in _LOCAL_ONLY_PREFIXES:
        if path.startswith(prefix):
            if is_loopback_ip(client_ip):
                return None
            logger.warning("deny local-only path: %s %s", client_ip, path)
            return Response(status_code=403)

    # 静态资源 → 放行(登录页面需要加载 CSS/JS)
    if _is_static_path(path):
        return None

    # 尝试提取用户身份
    user = _extract_user_from_scope(scope)

    # setup-token session 限定放行
    if user and user.get("sub") == SETUP_BOOTSTRAP_SUB:
        if path in _SETUP_ALLOWED_PATHS:
            scope["user"] = user
            logger.debug("allow setup-bootstrap: %s", path)
            return None
        # 根路径 → 强制跳到 setup 页(避免 React App 加载后所有 API 都 403 的烂体验)
        if path == "/" or path == "":
            return RedirectResponse(base_path + "/auth/setup", status_code=302)
        # 其他业务路径 → 403
        scope_type = scope.get("type")
        if scope_type == "websocket":
            logger.info("reject ws (setup-bootstrap restricted): %s", path)
            return Response(status_code=4401)
        logger.info("deny setup-bootstrap business path: %s", path)
        return Response(status_code=403)

    if user:
        # 认证通过,注入用户信息到 scope
        scope["user"] = user
        logger.debug("allow authenticated: %s %s", user.get("sub", "?"), path)
        return None

    # 未认证
    scope_type = scope.get("type")
    if scope_type == "websocket":
        logger.info("reject ws (unauthenticated): %s %s", client_ip, path)
        return Response(status_code=4401)

    # HTTP: 重定向到登录页(带上原路径作为 next)
    target = _login_redirect_target(base_path, path)
    logger.info("redirect to login: %s %s → %s", client_ip, path, target)
    return RedirectResponse(target, status_code=302)
