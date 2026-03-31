"""mutbot.auth.views — 认证路由（View 子类，自动发现注册）。

CallbackView      /auth/callback       — 直连 OIDC 回调
RelayCallbackView /auth/relay-callback — 中转认证回调（接收断言 JWT）
LogoutView        /auth/logout         — 退出登录
UserinfoView      /auth/userinfo       — 当前用户信息
ProvidersView     /auth/providers      — 可用登录选项列表（前端登录页使用）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode, quote

from mutagent.net.server import View, Request, Response, json_response, html_response

from mutbot.auth.token import (
    COOKIE_NAME,
    create_session_token,
    verify_session_token,
    verify_relay_assertion,
    set_session_cookie,
    clear_session_cookie,
    extract_token_from_cookie,
)
from mutbot.auth.providers import (
    OIDCProvider,
    create_provider_from_config,
)

logger = logging.getLogger(__name__)


def _get_auth_config() -> dict[str, Any] | None:
    """获取 auth 配置。返回 None 表示未配置认证。"""
    from mutbot.web import server as _server_mod
    cfg = _server_mod.config
    if cfg is None:
        return None
    return cfg.get("auth")


def _get_providers() -> dict[str, OIDCProvider]:
    """从配置创建所有直连 Provider。"""
    auth = _get_auth_config()
    if not auth:
        return {}
    providers_cfg = auth.get("providers", {})
    result = {}
    for name, cfg in providers_cfg.items():
        try:
            result[name] = create_provider_from_config(name, cfg)
        except Exception as e:
            logger.error("创建 Provider '%s' 失败: %s", name, e)
    return result


def _get_relay_config() -> dict[str, Any] | None:
    """获取 relay 中转站配置。"""
    auth = _get_auth_config()
    if not auth:
        return None
    relay = auth.get("relay")
    if not relay:
        return None
    return {"url": relay}


def _get_allowed_users() -> list[str] | None:
    """获取白名单。None 表示未配置（允许所有认证用户）。"""
    auth = _get_auth_config()
    if not auth:
        return None
    return auth.get("allowed_users")


def _get_session_ttl() -> int:
    """获取 session 有效期。"""
    auth = _get_auth_config()
    if auth:
        return auth.get("session_ttl", 604800)
    return 604800


def _is_secure(request: Request) -> bool:
    """判断请求是否通过 HTTPS。"""
    # 检查常见的 HTTPS 标记
    proto = request.headers.get("x-forwarded-proto", "")
    return proto == "https"


def _get_callback_url(request: Request, path: str) -> str:
    """根据请求构建回调 URL。"""
    host = request.headers.get("host", "localhost:8741")
    scheme = "https" if _is_secure(request) else "http"
    base_path = ""
    from mutbot.web import server as _server_mod
    if _server_mod.config is not None:
        base_path = _server_mod.config.get("base_path", default="") or ""
    return f"{scheme}://{host}{base_path}{path}"


# ---------------------------------------------------------------------------
# Nonce 管理（HMAC 自验证，无状态）
# ---------------------------------------------------------------------------


def _create_nonce() -> str:
    """创建 nonce（含签名和时间戳，可自验证）。"""
    from mutbot.auth.token import _get_session_secret
    ts = str(int(time.time()))
    rand = secrets.token_urlsafe(16)
    data = f"{ts}:{rand}"
    sig = hmac.new(_get_session_secret().encode(), data.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{data}:{sig}"


def _verify_nonce(nonce: str, max_age: int = 600) -> bool:
    """验证 nonce（检查签名和过期，默认 10 分钟有效）。"""
    from mutbot.auth.token import _get_session_secret
    parts = nonce.split(":")
    if len(parts) != 3:
        return False
    ts, rand, sig = parts
    try:
        age = int(time.time()) - int(ts)
        if age > max_age or age < 0:
            return False
    except ValueError:
        return False
    data = f"{ts}:{rand}"
    expected = hmac.new(_get_session_secret().encode(), data.encode(), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# LoginView — 登录页面
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CallbackView — 直连 OIDC 回调
# ---------------------------------------------------------------------------


class CallbackView(View):
    """直连 OIDC Provider 回调。

    action=start: 发起授权（重定向到 Provider）
    code=xxx: Provider 回调（换取 token + 签发 session）
    """
    path = "/auth/callback"

    async def get(self, request: Request) -> Response:
        provider_name = request.query_params.get("provider", "")
        action = request.query_params.get("action", "")

        # 发起授权
        if action == "start":
            providers = _get_providers()
            provider = providers.get(provider_name)
            if not provider:
                return json_response({"error": f"unknown provider: {provider_name}"}, status=400)
            state = _create_nonce() + "|" + provider_name
            redirect_uri = _get_callback_url(request, "/auth/callback")
            url = provider.authorize_url(redirect_uri, state)
            return Response(status=302, headers={"location": url})

        # Provider 回调
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        if not code or not state:
            return json_response({"error": "missing code or state"}, status=400)

        # 解析 state
        parts = state.split("|", 1)
        if len(parts) != 2:
            return json_response({"error": "invalid state"}, status=400)
        nonce, provider_name = parts
        if not _verify_nonce(nonce):
            return json_response({"error": "invalid or expired state"}, status=400)

        providers = _get_providers()
        provider = providers.get(provider_name)
        if not provider:
            return json_response({"error": f"unknown provider: {provider_name}"}, status=400)

        try:
            redirect_uri = _get_callback_url(request, "/auth/callback")
            access_token = await provider.exchange_code(code, redirect_uri)
            userinfo = await provider.get_userinfo(access_token)
        except Exception as e:
            logger.error("OIDC 回调失败: %s", e)
            return json_response({"error": "authentication failed"}, status=500)

        # 白名单检查
        allowed = _get_allowed_users()
        if allowed is not None and userinfo.sub not in allowed:
            from html import escape
            return html_response(
                f"<h1>403 Forbidden</h1><p>User {escape(userinfo.sub)} is not allowed.</p>",
                status=403,
            )

        # 签发 session
        token = create_session_token(
            sub=userinfo.sub,
            name=userinfo.name,
            avatar=userinfo.avatar,
            provider=userinfo.provider,
            ttl=_get_session_ttl(),
        )
        headers: dict[str, str] = {"location": "/"}
        set_session_cookie(headers, token, secure=_is_secure(request))
        return Response(status=302, headers=headers)


# ---------------------------------------------------------------------------
# RelayCallbackView — 中转认证回调
# ---------------------------------------------------------------------------


class RelayCallbackView(View):
    """中转认证回调。

    中转站回跳时 assertion 在 URL fragment 中（#assertion=JWT），
    此 View 返回一个 HTML 页面，JS 提取 fragment 后 POST 到自身。
    """
    path = "/auth/relay-callback"

    async def get(self, request: Request) -> Response:
        """返回中转页面，JS 从 fragment 提取 assertion 并 POST。"""
        return html_response(_RELAY_CALLBACK_HTML)

    async def post(self, request: Request) -> Response:
        """接收 assertion JWT，验证后签发 session。

        支持两种模式：
        - 正常模式：auth 已配置，从 config 读取 relay URL
        - Setup 模式：auth 未配置，从临时 nonce 状态获取 relay URL 并保存配置
        """
        try:
            body = await request.json()
            assertion = body.get("assertion", "")
        except Exception:
            return json_response({"error": "invalid body"}, status=400)

        if not assertion:
            return json_response({"error": "missing assertion"}, status=400)

        # 尝试从 assertion 中提取 nonce（未验签，仅用于查找 relay URL）
        import jwt as _jwt
        try:
            unverified = _jwt.decode(assertion, options={"verify_signature": False})
        except Exception:
            return json_response({"error": "invalid assertion format"}, status=400)

        nonce = unverified.get("nonce", "")

        # 确定 relay URL（正常模式 vs setup 模式）
        relay = _get_relay_config()
        setup_info = None

        if relay:
            relay_url = relay["url"].rstrip("/")
        else:
            # Setup 模式：从临时 nonce 状态查找
            from mutbot.auth.setup import pop_setup_nonce
            setup_info = pop_setup_nonce(nonce)
            if setup_info:
                relay_url = setup_info["relay_url"].rstrip("/")
            else:
                return json_response({"error": "relay not configured"}, status=400)

        # 获取中转站公钥
        public_key = await _fetch_relay_public_key(relay_url)
        if not public_key:
            return json_response({"error": "failed to fetch relay public key"}, status=500)

        # 验证断言
        payload = verify_relay_assertion(assertion, public_key)
        if not payload:
            return json_response({"error": "invalid assertion"}, status=401)

        # 验证 nonce
        if not _verify_nonce(nonce):
            return json_response({"error": "invalid or expired nonce"}, status=401)

        # 验证 audience
        expected_aud = _get_callback_url(request, "/auth/relay-callback")
        if payload.get("aud") != expected_aud:
            logger.warning("audience 不匹配: %s != %s", payload.get("aud"), expected_aud)
            return json_response({"error": "audience mismatch"}, status=401)

        sub = payload.get("sub", "")

        # Setup 模式：保存 auth 配置
        if setup_info:
            from mutbot.auth.setup import save_auth_config
            save_auth_config(relay_url, setup_info["access_mode"], sub)

        # 白名单检查（setup 模式下刚保存的配置已生效）
        allowed = _get_allowed_users()
        if allowed is not None and sub not in allowed:
            return json_response({"error": f"user {sub} not allowed"}, status=403)

        # 签发 session
        token = create_session_token(
            sub=sub,
            name=payload.get("name", ""),
            avatar=payload.get("avatar", ""),
            provider=payload.get("provider", ""),
            ttl=_get_session_ttl(),
        )
        resp_data = {"ok": True}
        headers: dict[str, str] = {"content-type": "application/json; charset=utf-8"}
        set_session_cookie(headers, token, secure=_is_secure(request))
        return Response(
            status=200,
            body=json.dumps(resp_data).encode(),
            headers=headers,
        )


async def _fetch_relay_providers(relay_url: str) -> list[str]:
    """从中转站元信息获取支持的 provider 列表。"""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{relay_url}/.well-known/mutbot-relay.json", timeout=10)
            data = resp.json()
            return data.get("providers", [])
    except Exception as e:
        logger.error("获取 relay provider 列表失败: %s", e)
        return []


async def _fetch_relay_public_key(relay_url: str) -> str | None:
    """从中转站元信息获取 Ed25519 公钥。"""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{relay_url}/.well-known/mutbot-relay.json", timeout=10)
            data = resp.json()
            return data.get("public_key")
    except Exception as e:
        logger.error("获取 relay 公钥失败: %s", e)
        return None


_RELAY_CALLBACK_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MutBot</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; margin: 0; background: #1f1f1f; color: #858585; }
  .error { color: #f14c4c; font-size: 14px; }
  a { color: #569cd6; text-decoration: none; margin-top: 12px; display: inline-block; }
  a:hover { text-decoration: underline; }
  .container { text-align: center; }
</style>
</head>
<body>
<div class="container">
<div id="msg">Signing in...</div>
<div id="back" style="display:none"><a href="/">&#8592; Back to MutBot</a></div>
</div>
<script>
(async () => {
  const hash = location.hash.substring(1);
  const params = new URLSearchParams(hash);
  const assertion = params.get('assertion');
  const basePath = location.pathname.replace(/\\/auth\\/relay-callback$/, '') || '/';
  if (!assertion) {
    const el = document.getElementById('msg');
    el.className = 'error';
    el.textContent = 'Authentication failed: missing assertion';
    document.getElementById('back').style.display = '';
    document.querySelector('#back a').href = basePath;
    return;
  }
  try {
    const resp = await fetch(location.pathname, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ assertion }),
    });
    if (resp.ok) {
      location.href = basePath;
    } else {
      const data = await resp.json();
      const el = document.getElementById('msg');
      el.className = 'error';
      el.textContent = 'Authentication failed: ' + (data.error || resp.statusText);
      document.getElementById('back').style.display = '';
      document.querySelector('#back a').href = basePath;
    }
  } catch (e) {
    const el = document.getElementById('msg');
    el.className = 'error';
    el.textContent = 'Authentication failed: ' + e.message;
    document.getElementById('back').style.display = '';
    document.querySelector('#back a').href = basePath;
  }
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# LogoutView — 退出登录
# ---------------------------------------------------------------------------


class LogoutView(View):
    """退出登录，清除 session cookie。"""
    path = "/auth/logout"

    async def get(self, request: Request) -> Response:
        base_path = ""
        from mutbot.web import server as _server_mod
        if _server_mod.config is not None:
            base_path = _server_mod.config.get("base_path", default="") or ""
        headers: dict[str, str] = {"location": f"{base_path}/"}
        clear_session_cookie(headers, secure=_is_secure(request))
        return Response(status=302, headers=headers)


# ---------------------------------------------------------------------------
# UserinfoView — 当前用户信息
# ---------------------------------------------------------------------------


class UserinfoView(View):
    """返回当前用户信息（JSON）。"""
    path = "/auth/userinfo"

    async def get(self, request: Request) -> Response:
        cookie = request.headers.get("cookie", "")
        token = extract_token_from_cookie(cookie)
        if not token:
            return json_response({"error": "not authenticated"}, status=401)
        payload = verify_session_token(token)
        if not payload:
            return json_response({"error": "invalid token"}, status=401)
        return json_response({
            "sub": payload.get("sub"),
            "name": payload.get("name"),
            "avatar": payload.get("avatar"),
            "provider": payload.get("provider"),
        })


class ProvidersView(View):
    """返回可用的登录选项（JSON）。前端登录页使用。"""
    path = "/auth/providers"

    async def get(self, request: Request) -> Response:
        auth = _get_auth_config()
        if not auth:
            return json_response({"providers": [], "auth_enabled": False})

        # auth 存在但无登录方式（如仅有 relay_service）→ 视为未启用
        if not auth.get("relay") and not auth.get("providers"):
            return json_response({"providers": [], "auth_enabled": False})

        options: list[dict[str, str]] = []

        # 直连 Provider
        providers = _get_providers()
        for name in providers:
            options.append({
                "name": name,
                "label": name.replace("-", " ").replace("_", " ").title(),
                "type": "direct",
                "url": f"/auth/callback?provider={name}&action=start",
            })

        # 中转站 Provider — 从元信息动态获取 provider 列表
        relay = _get_relay_config()
        relay_domain = ""
        if relay:
            relay_url = relay["url"].rstrip("/")
            relay_domain = relay_url.split("//", 1)[-1]
            relay_providers = await _fetch_relay_providers(relay_url)
            callback = _get_callback_url(request, "/auth/relay-callback")
            for rp in relay_providers:
                nonce = _create_nonce()
                options.append({
                    "name": rp,
                    "label": rp.replace("-", " ").replace("_", " ").title(),
                    "type": "relay",
                    "url": f"{relay_url}/auth/start?callback={quote(callback)}&provider={quote(rp)}&nonce={nonce}",
                })

        return json_response({
            "providers": options,
            "auth_enabled": True,
            "relay_domain": relay_domain,
        })


# ---------------------------------------------------------------------------
# AuthSetupView — 服务端渲染 Auth 配置页面
# ---------------------------------------------------------------------------


class AuthSetupView(View):
    """Auth 配置引导页面（服务端渲染，不依赖 React 前端）。

    GET /auth/setup          → 渲染 HTML 页面
    POST /auth/setup         → 处理表单提交（token 验证 / relay 配置 / OAuth 跳转）
    """
    path = "/auth/setup"

    async def get(self, request: Request) -> Response:
        """渲染 setup 页面。

        本地请求 → 直接显示配置表单
        远程请求 → 先显示 token 输入（验证通过后通过 cookie 记住状态）
        """
        from mutbot.auth.network import is_loopback_ip
        from mutbot.auth.middleware import current_client_ip
        import mutbot.auth.setup_token as setup_token

        # 已配置 auth → 显示已配置提示
        auth = _get_auth_config()
        if auth and (auth.get("relay") or auth.get("providers")):
            return html_response(_render_setup_page(
                step="already_configured",
                relay_url=auth.get("relay", ""),
            ))

        # 判断是否本地请求
        client_ip = current_client_ip.get()
        is_local = is_loopback_ip(client_ip) if client_ip else True

        # 本地请求或无活跃 token → 直接显示配置表单
        if is_local or not setup_token.is_active():
            return html_response(_render_setup_page(step="configure"))

        # 远程请求 → 检查 cookie 中的验证状态
        cookie = request.headers.get("cookie", "")
        if _check_setup_verified_cookie(cookie):
            return html_response(_render_setup_page(step="configure"))

        # 远程请求，未验证 → 显示 token 输入
        return html_response(_render_setup_page(step="token_input"))

    async def post(self, request: Request) -> Response:
        """处理表单提交。

        action=verify_token  → 验证 token，成功则显示配置表单
        action=connect_relay → 校验 relay URL，获取 provider 列表，显示选择页
        action=start_oauth   → 用户选择 provider 后，创建 nonce，重定向到 OAuth
        """
        try:
            body = await request.body()
            from urllib.parse import parse_qs
            form = parse_qs(body.decode("utf-8"))
        except Exception:
            return html_response(_render_setup_page(step="token_input", error="Invalid form data"))

        action = (form.get("action", [""])[0])

        if action == "verify_token":
            return await self._handle_verify_token(request, form)
        elif action == "connect_relay":
            return await self._handle_connect_relay(request, form)
        elif action == "start_oauth":
            return await self._handle_start_oauth(request, form)

        return html_response(_render_setup_page(step="token_input", error="Unknown action"))

    async def _handle_verify_token(self, request: Request, form: dict) -> Response:
        import mutbot.auth.setup_token as setup_token

        token = (form.get("token", [""])[0]).strip()
        if not token:
            return html_response(_render_setup_page(step="token_input", error="Please enter the setup token"))

        if not setup_token.verify(token):
            return html_response(_render_setup_page(step="token_input", error="Invalid token. Check the server console output."))

        # 验证通过 → 设置短期 httponly cookie，后续请求通过 cookie 判断已验证
        resp = html_response(_render_setup_page(step="configure"))
        _set_setup_verified_cookie(resp, secure=_is_secure(request))
        return resp

    async def _handle_connect_relay(self, request: Request, form: dict) -> Response:
        """校验 relay URL，获取 provider 列表，渲染选择页。"""
        import mutbot.auth.setup_token as setup_token
        from mutbot.auth.network import is_loopback_ip
        from mutbot.auth.middleware import current_client_ip

        relay_url = (form.get("relay_url", ["https://mutbot.ai"])[0]).strip().rstrip("/")

        # 远程请求需验证（通过 cookie 判断已验证）
        client_ip = current_client_ip.get()
        is_local = is_loopback_ip(client_ip) if client_ip else True

        if not is_local and setup_token.is_active():
            cookie = request.headers.get("cookie", "")
            if not _check_setup_verified_cookie(cookie):
                return html_response(_render_setup_page(step="token_input", error="Token expired or invalid. Please re-enter."))

        # 校验 relay_url（防止 SSRF：scheme 必须是 https，或 http://localhost 用于开发）
        ssrf_error = _validate_relay_url(relay_url)
        if ssrf_error:
            return html_response(_render_setup_page(
                step="configure",
                error=ssrf_error,
            ))

        # 从 relay 获取 provider 列表
        provider_names = await _fetch_relay_providers(relay_url)
        if not provider_names:
            return html_response(_render_setup_page(
                step="configure",
                error=f"Cannot connect to relay server: {relay_url}",
            ))

        # 构造 provider 显示列表
        providers = [
            {"name": name, "label": name.replace("-", " ").replace("_", " ").title()}
            for name in provider_names
        ]

        return html_response(_render_setup_page(
            step="select_provider",
            relay_url=relay_url,
            providers=providers,
        ))

    async def _handle_start_oauth(self, request: Request, form: dict) -> Response:
        """用户选择 provider 后，创建 nonce，重定向到 relay OAuth。"""
        import mutbot.auth.setup_token as setup_token
        from mutbot.auth.setup import store_setup_nonce
        from mutbot.auth.network import is_loopback_ip
        from mutbot.auth.middleware import current_client_ip

        relay_url = (form.get("relay_url", ["https://mutbot.ai"])[0]).strip().rstrip("/")
        provider = (form.get("provider", [""])[0]).strip()

        if not provider:
            return html_response(_render_setup_page(step="configure", error="No provider selected."))

        # 远程请求需验证
        client_ip = current_client_ip.get()
        is_local = is_loopback_ip(client_ip) if client_ip else True

        if not is_local and setup_token.is_active():
            cookie = request.headers.get("cookie", "")
            if not _check_setup_verified_cookie(cookie):
                return html_response(_render_setup_page(step="token_input", error="Token expired or invalid. Please re-enter."))

        # 创建 nonce 并存储（access_mode 固定为 only_me）
        nonce = _create_nonce()
        store_setup_nonce(nonce, relay_url, "only_me")

        # 构造回调和登录 URL
        callback_url = _get_callback_url(request, "/auth/relay-callback")
        login_url = (
            f"{relay_url}/auth/start"
            f"?callback={quote(callback_url)}"
            f"&provider={quote(provider)}"
            f"&nonce={nonce}"
        )

        return Response(status=302, headers={"location": login_url})


# ---------------------------------------------------------------------------
# relay_url SSRF 防护
# ---------------------------------------------------------------------------


def _validate_relay_url(url: str) -> str | None:
    """校验 relay_url 是否安全。返回错误消息或 None（通过）。

    规则：scheme 必须是 https，或 http://localhost / http://127.0.0.1 用于开发。
    拒绝私有 IP 段，防止 SSRF 攻击。
    """
    from urllib.parse import urlparse
    import ipaddress

    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL format."

    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()

    if not hostname:
        return "Invalid URL: missing hostname."

    # 允许 http://localhost 和 http://127.0.0.1 用于开发
    if scheme == "http":
        if hostname not in ("localhost", "127.0.0.1", "::1"):
            return "Relay URL must use HTTPS (http is only allowed for localhost)."
        return None

    if scheme != "https":
        return "Relay URL must use HTTPS."

    # HTTPS 场景：检查 hostname 是否指向私有 IP
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local:
            return "Relay URL must not point to a private/reserved IP address."
    except ValueError:
        # hostname 是域名，不是 IP → 允许（DNS 解析后的 SSRF 防护交给 httpx 或网络层）
        pass

    return None


# ---------------------------------------------------------------------------
# Setup verified cookie 辅助（短期 httponly cookie，验证 setup token 后设置）
# ---------------------------------------------------------------------------

_SETUP_COOKIE_NAME = "mutbot_setup_verified"
_SETUP_COOKIE_MAX_AGE = 300  # 5 分钟


def _set_setup_verified_cookie(resp: Response, *, secure: bool = False) -> None:
    """设置短期 httponly cookie 标记 setup token 已验证。"""
    flags = "HttpOnly; SameSite=Lax; Path=/auth/setup"
    if secure:
        flags += "; Secure"
    cookie_val = f"{_SETUP_COOKIE_NAME}=1; Max-Age={_SETUP_COOKIE_MAX_AGE}; {flags}"
    # Response headers 是 dict，set-cookie 需要追加
    if hasattr(resp, 'headers') and isinstance(resp.headers, dict):
        resp.headers["set-cookie"] = cookie_val


def _check_setup_verified_cookie(cookie_header: str) -> bool:
    """检查请求中是否携带有效的 setup verified cookie。"""
    if not cookie_header:
        return False
    from http.cookies import SimpleCookie
    try:
        c = SimpleCookie(cookie_header)
        return _SETUP_COOKIE_NAME in c and c[_SETUP_COOKIE_NAME].value == "1"
    except Exception:
        return False


def _render_setup_page(
    step: str = "token_input",
    error: str = "",
    relay_url: str = "",
    providers: list[dict[str, str]] | None = None,
) -> str:
    """渲染 auth setup HTML 页面。"""
    from html import escape

    error_html = ""
    if error:
        error_html = f'<div class="error">{escape(error)}</div>'

    if step == "already_configured":
        content = f"""
        <h2>Authentication Configured</h2>
        <p class="hint">Auth is already set up{(' via <strong>' + escape(relay_url) + '</strong>') if relay_url else ''}.</p>
        <a href="/" class="btn">Back to MutBot</a>
        """
    elif step == "token_input":
        content = f"""
        <h2>Setup Token Required</h2>
        <p class="hint">This server has no authentication configured. Enter the setup token from the server console to continue.</p>
        {error_html}
        <form method="POST">
            <input type="hidden" name="action" value="verify_token">
            <label for="token">Setup Token</label>
            <input type="text" id="token" name="token" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                   autocomplete="off" autofocus required>
            <button type="submit" class="btn primary">Verify</button>
        </form>
        """
    elif step == "configure":
        content = f"""
        <h2>Configure Authentication</h2>
        <p class="hint">Set up login to control who can access this MutBot server.</p>
        {error_html}
        <form method="POST">
            <input type="hidden" name="action" value="connect_relay">
            <label for="relay_url">Relay Server</label>
            <input type="text" id="relay_url" name="relay_url" value="https://mutbot.ai"
                   placeholder="https://mutbot.ai">
            <p class="field-hint">Uses a relay server for zero-config login. No registration needed.</p>

            <button type="submit" class="btn primary">Connect &rarr;</button>
        </form>
        """
    elif step == "select_provider":
        provider_buttons = ""
        for p in (providers or []):
            name = escape(p["name"])
            label = escape(p["label"])
            url = escape(relay_url)
            provider_buttons += f"""
            <form method="POST" style="margin-bottom: 8px;">
                <input type="hidden" name="action" value="start_oauth">
                <input type="hidden" name="relay_url" value="{url}">
                <input type="hidden" name="provider" value="{name}">
                <button type="submit" class="btn primary provider-btn">Sign in with {label} &rarr;</button>
            </form>"""
        relay_hint = f'<p class="field-hint" style="text-align:center; margin-top: 16px;">via {escape(relay_url)}</p>' if relay_url else ""
        content = f"""
        <h2>Choose Login Provider</h2>
        <p class="hint">Select a provider to sign in.</p>
        {error_html}
        <div class="provider-list">
            {provider_buttons}
        </div>
        {relay_hint}
        <div style="margin-top: 16px; text-align: center;">
            <a href="/auth/setup" class="back-link">&larr; Back</a>
        </div>
        """
    else:
        content = "<p>Unknown step</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MutBot Setup</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
    background: #1a1a1a;
    color: #d4d4d4;
    margin: 0;
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
  }}
  .container {{
    background: #252525;
    border: 1px solid #333;
    border-radius: 12px;
    padding: 40px;
    max-width: 440px;
    width: 100%;
    margin: 20px;
  }}
  h2 {{
    margin: 0 0 8px 0;
    font-size: 20px;
    font-weight: 600;
    color: #e5e5e5;
  }}
  .hint {{
    color: #858585;
    font-size: 14px;
    line-height: 1.5;
    margin: 0 0 24px 0;
  }}
  .field-hint {{
    color: #666;
    font-size: 12px;
    margin: 4px 0 16px 0;
  }}
  .error {{
    background: rgba(241, 76, 76, 0.1);
    border: 1px solid rgba(241, 76, 76, 0.3);
    border-radius: 6px;
    color: #f14c4c;
    padding: 10px 14px;
    font-size: 13px;
    margin-bottom: 16px;
  }}
  label {{
    display: block;
    font-size: 13px;
    font-weight: 500;
    color: #b0b0b0;
    margin-bottom: 6px;
  }}
  input[type="text"] {{
    width: 100%;
    padding: 10px 12px;
    background: #1a1a1a;
    border: 1px solid #404040;
    border-radius: 6px;
    color: #d4d4d4;
    font-size: 14px;
    font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
    outline: none;
    transition: border-color 0.15s;
    margin-bottom: 16px;
  }}
  input[type="text"]:focus {{
    border-color: #569cd6;
  }}
  .radio-group {{
    margin-bottom: 20px;
  }}
  .radio-label {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 0;
    cursor: pointer;
    font-size: 14px;
    color: #d4d4d4;
  }}
  .radio-label input[type="radio"] {{
    accent-color: #569cd6;
  }}
  .btn {{
    display: inline-block;
    padding: 10px 20px;
    border-radius: 6px;
    font-size: 14px;
    font-weight: 500;
    text-decoration: none;
    border: 1px solid #404040;
    background: #333;
    color: #d4d4d4;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }}
  .btn:hover {{
    background: #3a3a3a;
    border-color: #555;
  }}
  .btn.primary {{
    background: #264f78;
    border-color: #569cd6;
    color: #fff;
  }}
  .btn.primary:hover {{
    background: #2d5f8a;
  }}
  .provider-btn {{
    width: 100%;
  }}
  .back-link {{
    color: #858585;
    text-decoration: none;
    font-size: 13px;
  }}
  .back-link:hover {{
    color: #b0b0b0;
  }}
</style>
</head>
<body>
<div class="container">
{content}
</div>
</body>
</html>"""
