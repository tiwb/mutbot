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
from urllib.parse import quote

from mutio.codec.json import JsonObject, get_field, narrow_value
from mutio.net.server import HTMLResponse, JSONResponse, RedirectResponse, Request, Response, View

from mutbot.auth.token import (
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
                return JSONResponse({"error": f"unknown provider: {provider_name}"}, status_code=400)
            state = _create_nonce() + "|" + provider_name
            redirect_uri = _get_callback_url(request, "/auth/callback")
            url = provider.authorize_url(redirect_uri, state)
            return RedirectResponse(url, status_code=302)

        # Provider 回调
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        if not code or not state:
            return JSONResponse({"error": "missing code or state"}, status_code=400)

        # 解析 state
        parts = state.split("|", 1)
        if len(parts) != 2:
            return JSONResponse({"error": "invalid state"}, status_code=400)
        nonce, provider_name = parts
        if not _verify_nonce(nonce):
            return JSONResponse({"error": "invalid or expired state"}, status_code=400)

        providers = _get_providers()
        provider = providers.get(provider_name)
        if not provider:
            return JSONResponse({"error": f"unknown provider: {provider_name}"}, status_code=400)

        try:
            redirect_uri = _get_callback_url(request, "/auth/callback")
            access_token = await provider.exchange_code(code, redirect_uri)
            userinfo = await provider.get_userinfo(access_token)
        except Exception as e:
            logger.error("OIDC 回调失败: %s", e)
            return JSONResponse({"error": "authentication failed"}, status_code=500)

        # 白名单检查
        allowed = _get_allowed_users()
        if allowed is not None and userinfo.sub not in allowed:
            from html import escape
            return HTMLResponse(
                f"<h1>403 Forbidden</h1><p>User {escape(userinfo.sub)} is not allowed.</p>",
                status_code=403,
            )

        # 签发 session
        token = create_session_token(
            sub=userinfo.sub,
            name=userinfo.name,
            avatar=userinfo.avatar,
            provider=userinfo.provider,
            ttl=_get_session_ttl(),
        )
        cookie_headers: dict[str, str] = {}
        set_session_cookie(cookie_headers, token, secure=_is_secure(request))
        return RedirectResponse("/", status_code=302, headers=cookie_headers)


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
        return HTMLResponse(_RELAY_CALLBACK_HTML)

    async def post(self, request: Request) -> Response:
        """接收 assertion JWT，验证后签发 session。

        支持两种模式：
        - 正常模式：auth 已配置，从 config 读取 relay URL
        - Setup 模式：auth 未配置，从临时 nonce 状态获取 relay URL 并保存配置
        """
        try:
            body = narrow_value(await request.json(), JsonObject)
            assertion = get_field(body, "assertion", str, default="")
        except Exception:
            return JSONResponse({"error": "invalid body"}, status_code=400)

        if not assertion:
            return JSONResponse({"error": "missing assertion"}, status_code=400)

        # 尝试从 assertion 中提取 nonce（未验签，仅用于查找 relay URL）
        import jwt as _jwt
        try:
            unverified = _jwt.decode(assertion, options={"verify_signature": False})
        except Exception:
            return JSONResponse({"error": "invalid assertion format"}, status_code=400)

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
                return JSONResponse({"error": "relay not configured"}, status_code=400)

        # 获取中转站公钥
        public_key = await _fetch_relay_public_key(relay_url)
        if not public_key:
            return JSONResponse({"error": "failed to fetch relay public key"}, status_code=500)

        # 验证断言
        payload = verify_relay_assertion(assertion, public_key)
        if not payload:
            return JSONResponse({"error": "invalid assertion"}, status_code=401)

        # 验证 nonce
        if not _verify_nonce(nonce):
            return JSONResponse({"error": "invalid or expired nonce"}, status_code=401)

        # 验证 audience
        expected_aud = _get_callback_url(request, "/auth/relay-callback")
        if payload.get("aud") != expected_aud:
            logger.warning("audience 不匹配: %s != %s", payload.get("aud"), expected_aud)
            return JSONResponse({"error": "audience mismatch"}, status_code=401)

        sub = payload.get("sub", "")

        # Setup 模式：保存 auth 配置
        if setup_info:
            from mutbot.auth.setup import save_auth_config
            save_auth_config(relay_url, setup_info["access_mode"], sub)

        # 白名单检查（setup 模式下刚保存的配置已生效）
        allowed = _get_allowed_users()
        if allowed is not None and sub not in allowed:
            return JSONResponse({"error": f"user {sub} not allowed"}, status_code=403)

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
            status_code=200,
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
        cookie_headers: dict[str, str] = {}
        clear_session_cookie(cookie_headers, secure=_is_secure(request))
        return RedirectResponse(f"{base_path}/", status_code=302, headers=cookie_headers)


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
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        payload = verify_session_token(token)
        if not payload:
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return JSONResponse({
            "sub": payload.get("sub"),
            "name": payload.get("name"),
            "avatar": payload.get("avatar"),
            "provider": payload.get("provider"),
        })


class ProvidersView(View):
    """返回可用的登录选项（JSON）。前端登录页使用。"""
    path = "/auth/providers"

    async def get(self, request: Request) -> Response:
        import mutbot.auth.setup_token as _setup_token

        auth = _get_auth_config()

        # setup token 激活时,无论 auth 是否配置都追加 setup-token 选项
        # 用于:首次配置 / reconfigure 后管理员锁出自己的恢复
        setup_option: dict[str, str] | None = None
        if _setup_token.is_active():
            setup_option = {
                "name": "setup-token",
                "label": "Setup Token",
                "type": "setup-token",
                "url": "/auth/setup-token-login",
            }

        if not auth:
            providers = [setup_option] if setup_option else []
            return JSONResponse({"providers": providers, "auth_enabled": False})

        # auth 存在但无登录方式(如仅有 relay_service)→ 视为未启用
        if not auth.get("relay") and not auth.get("providers"):
            providers = [setup_option] if setup_option else []
            return JSONResponse({"providers": providers, "auth_enabled": False})

        options: list[dict[str, str]] = []

        # setup-token 优先放在最前面(临时入口,显眼)
        if setup_option:
            options.append(setup_option)

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

        return JSONResponse({
            "providers": options,
            "auth_enabled": True,
            "relay_domain": relay_domain,
        })


# ---------------------------------------------------------------------------
# AuthSetupView — HTML 壳，挂载 mutgui 前端
# ---------------------------------------------------------------------------


_SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MutBot Setup</title>
<link rel="stylesheet" href="/setup.css">
<style>body{margin:0;background:#141414;color:#d4d4d4;font-family:system-ui,-apple-system,sans-serif;}</style>
</head>
<body>
<div id="app"></div>
<script src="/setup.js"></script>
</body>
</html>"""


class AuthSetupView(View):
    """Auth 配置引导页 — 返回挂载 mutgui 的 HTML 壳。

    实际向导逻辑在 `mutbot.auth.setup_view.AuthSetupView` (mutgui View)，
    通过 `/auth/setup/ws` WebSocket 端点驱动。
    """
    path = "/auth/setup"

    async def get(self, request: Request) -> Response:
        return HTMLResponse(_SETUP_HTML)


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

