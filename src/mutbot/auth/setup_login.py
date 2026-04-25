"""mutbot.auth.setup_login — Setup token 登录路由。

Setup token 不再是"setup 专用入场券",而是"未配置 auth 时可用的登录方式"。
验证通过签发标准 session token(sub=SETUP_BOOTSTRAP_SUB),
后续访问 /auth/setup 走通用鉴权流程。

详见 docs/specifications/refactor-setup-auth-gate.md。
"""

from __future__ import annotations

import logging
from html import escape
from urllib.parse import parse_qsl

from mutio.net.server import Request, Response, View, html_response

from mutbot.auth.token import create_session_token, set_session_cookie

logger = logging.getLogger(__name__)


# Setup token 颁发的临时身份 sub。middleware 对此 sub 做限定放行。
SETUP_BOOTSTRAP_SUB = "setup:bootstrap"

# Setup token session TTL(秒)— 1 小时,够走完 setup,不会太久。
SETUP_SESSION_TTL = 3600


def _is_secure(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", "") == "https"


def _safe_next(next_param: str) -> str:
    """校验 next 参数,只允许同源相对路径(防 open redirect)。"""
    if not next_param:
        return ""
    if not next_param.startswith("/"):
        return ""
    if next_param.startswith("//") or next_param.startswith("/\\"):
        return ""
    if "\n" in next_param or "\r" in next_param:
        return ""
    return next_param


def _render_form(*, error: str | None = None, next_url: str = "") -> str:
    error_html = (
        f'<div class="error">{escape(error)}</div>' if error else ""
    )
    next_field = (
        f'<input type="hidden" name="next" value="{escape(next_url)}">'
        if next_url else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MutBot — Setup Token Sign In</title>
<style>
  body {{ margin: 0; min-height: 100vh; background: #141414; color: #d4d4d4;
         font-family: system-ui, -apple-system, sans-serif;
         display: flex; justify-content: center; align-items: center; }}
  .card {{ max-width: 460px; width: 100%; margin: 24px;
           background: #1f1f1f; border: 1px solid #303030;
           border-radius: 12px; padding: 32px; box-sizing: border-box; }}
  h2 {{ margin: 0 0 8px; color: #fff; font-size: 20px; font-weight: 500; }}
  p.subtitle {{ margin: 0 0 24px; color: #858585; font-size: 14px; }}
  input[type=text] {{ width: 100%; box-sizing: border-box;
                       padding: 10px 12px; font-family: ui-monospace, monospace;
                       background: #141414; border: 1px solid #303030;
                       border-radius: 6px; color: #d4d4d4; font-size: 14px;
                       margin-bottom: 16px; outline: none; }}
  input[type=text]:focus {{ border-color: #1668dc; }}
  button {{ width: 100%; padding: 10px; background: #1668dc; color: #fff;
            border: none; border-radius: 6px; font-size: 14px; cursor: pointer; }}
  button:hover {{ background: #1554b3; }}
  .error {{ background: #2a1215; border: 1px solid #58181c; color: #f14c4c;
            padding: 8px 12px; border-radius: 6px; font-size: 13px;
            margin-bottom: 16px; }}
  .hint {{ color: #6b6b6b; font-size: 12px; margin-top: 16px; }}
</style>
</head>
<body>
  <div class="card">
    <h2>Setup Token Required</h2>
    <p class="subtitle">Enter the setup token printed on the server console to continue.</p>
    {error_html}
    <form method="post" action="/auth/setup-token-login">
      {next_field}
      <input type="text" name="token" autofocus required
             placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
             autocomplete="off" spellcheck="false">
      <button type="submit">Verify</button>
    </form>
    <div class="hint">For initial setup. This token grants temporary admin access only to configure authentication.</div>
  </div>
</body>
</html>"""


class SetupTokenLoginView(View):
    """Setup token 登录路由 — 验证后签发标准 session,跳到 /auth/setup。"""

    path = "/auth/setup-token-login"

    async def get(self, request: Request) -> Response:
        import mutbot.auth.setup_token as setup_token
        if not setup_token.is_active():
            # token 已被消费(auth 已配置 / 主动失效)— 跳回主登录页
            return Response(status=302, headers={"location": "/auth/login"})
        next_url = _safe_next(request.query_params.get("next", ""))
        return html_response(_render_form(next_url=next_url))

    async def post(self, request: Request) -> Response:
        import mutbot.auth.setup_token as setup_token
        if not setup_token.is_active():
            return Response(status=302, headers={"location": "/auth/login"})

        body = await request.body()
        form = dict(parse_qsl(body.decode("utf-8", errors="replace")))
        token = (form.get("token") or "").strip()
        next_url = _safe_next(form.get("next") or "")

        if not token:
            return html_response(_render_form(error="Please enter a token", next_url=next_url), status=400)

        if not setup_token.verify(token):
            logger.info("setup-token login failed: invalid token")
            return html_response(_render_form(error="Invalid token", next_url=next_url), status=401)

        session_token = create_session_token(
            sub=SETUP_BOOTSTRAP_SUB,
            name="Setup Admin",
            avatar="",
            provider="setup-token",
            ttl=SETUP_SESSION_TTL,
        )
        # next 优先(如来自 /auth/login?next=/auth/setup),否则默认 /auth/setup
        target = next_url if next_url else "/auth/setup"
        headers: dict[str, str] = {"location": target}
        set_session_cookie(headers, session_token, secure=_is_secure(request))
        logger.info("setup-token login succeeded; session issued (sub=%s) → %s",
                    SETUP_BOOTSTRAP_SUB, target)
        return Response(status=302, headers=headers)


__all__ = ["SetupTokenLoginView", "SETUP_BOOTSTRAP_SUB", "SETUP_SESSION_TTL"]
