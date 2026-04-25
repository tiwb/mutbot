"""mutbot.auth.login_view — 独立登录页面 `/auth/login`(服务端渲染)。

设计目的:把登录入口从根目录 React App 中解耦出来。

- `/auth/login` → 纯 HTML 登录页(暗色主题,内联 CSS+JS,不依赖 React/mutgui)
- 登录页 JS fetch `/auth/providers` → 渲染按钮(数据驱动,与现有 LoginPage.tsx 一致)
- 支持 `?next=<path>`,登录成功后跳转;否则默认 `/`

`/auth` 和 `/auth/` 的 302 由 middleware 直接处理(纯 URL 规范化,无需经过 View)。

为什么不用 mutgui:鉴权最后一步必须设 cookie + 302,WebSocket 做不了。
未来 mutgui 基础设施完备(内置 redirect / OTT 桥接)后再重构。
"""

from __future__ import annotations

import logging
from html import escape

from mutio.net.server import HTMLResponse, Request, Response, View

logger = logging.getLogger(__name__)


def _safe_next(next_param: str) -> str:
    """校验 next 参数,只允许同源相对路径 — 防 open redirect。

    放行: /auth/setup, /api/health, /
    拒绝: //evil.com/x, https://evil.com, javascript:alert(1)
    """
    if not next_param:
        return ""
    # 必须以 / 开头且不是 // 或 /\(协议相对 URL)
    if not next_param.startswith("/"):
        return ""
    if next_param.startswith("//") or next_param.startswith("/\\"):
        return ""
    # 拒绝 fragment 注入诡异路径(简单粗暴)
    if "\n" in next_param or "\r" in next_param:
        return ""
    return next_param


def _render_login(*, next_url: str, message: str = "") -> str:
    next_attr = escape(next_url)
    msg_html = (
        f'<div class="msg">{escape(message)}</div>' if message else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MutBot — Sign In</title>
<style>
  body {{ margin: 0; min-height: 100vh; background: #141414; color: #d4d4d4;
         font-family: system-ui, -apple-system, sans-serif;
         display: flex; justify-content: center; align-items: center; }}
  .card {{ max-width: 420px; width: 100%; margin: 24px;
           background: #1f1f1f; border: 1px solid #303030;
           border-radius: 12px; padding: 32px; box-sizing: border-box; }}
  .title {{ margin: 0; color: #fff; font-size: 28px; font-weight: 500;
            text-align: center; letter-spacing: 0.5px; }}
  .subtitle {{ margin: 6px 0 28px; color: #858585; font-size: 13px;
              text-align: center; }}
  .msg {{ background: #2a1c0e; border: 1px solid #594214; color: #d4a64a;
          padding: 8px 12px; border-radius: 6px; font-size: 13px;
          margin-bottom: 16px; }}
  .providers {{ display: flex; flex-direction: column; gap: 10px; }}
  .btn {{ display: flex; align-items: center; justify-content: center;
          gap: 10px; padding: 11px 16px; background: #262626;
          border: 1px solid #303030; border-radius: 6px; color: #d4d4d4;
          text-decoration: none; font-size: 14px; cursor: pointer;
          transition: background 0.15s; }}
  .btn:hover {{ background: #2f2f2f; }}
  .btn.setup {{ border-style: dashed; }}
  .btn svg {{ flex-shrink: 0; }}
  .empty {{ color: #858585; font-size: 13px; text-align: center;
            padding: 12px 0; }}
  .hint {{ color: #6b6b6b; font-size: 12px; margin-top: 18px;
           padding-top: 16px; border-top: 1px solid #2a2a2a;
           line-height: 1.5; }}
  .relay {{ color: #6b6b6b; font-size: 11px; text-align: center;
            margin-top: 14px; }}
  .current-user {{ background: #1a2738; border: 1px solid #2a4365;
                   color: #9cc4ff; padding: 10px 12px; border-radius: 6px;
                   font-size: 13px; margin-bottom: 16px;
                   display: flex; justify-content: space-between; align-items: center; }}
  .current-user a {{ color: #d4d4d4; text-decoration: none;
                     padding: 4px 10px; background: #303030; border-radius: 4px;
                     font-size: 12px; }}
  .current-user a:hover {{ background: #404040; }}
  .spinner {{ display: inline-block; width: 14px; height: 14px;
              border: 2px solid #303030; border-top-color: #858585;
              border-radius: 50%; animation: spin 0.8s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
  <div class="card">
    <h1 class="title">MutBot</h1>
    <p class="subtitle">Sign in to continue</p>
    {msg_html}
    <div id="current-user" class="current-user" style="display:none">
      <span id="current-user-text"></span>
      <a href="/auth/logout">Sign out</a>
    </div>
    <div id="providers" class="providers">
      <div class="empty"><span class="spinner"></span> Loading sign-in options...</div>
    </div>
    <div id="hint" class="hint" style="display:none">
      Setup Token: temporary admin access for initial configuration.
      Requires server console access.
    </div>
    <div id="relay" class="relay" style="display:none"></div>
  </div>
<script>
(async () => {{
  const NEXT = {repr(next_url)};
  const ICONS = {{
    github: '<svg width=\"18\" height=\"18\" viewBox=\"0 0 16 16\" fill=\"currentColor\"><path d=\"M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z\"/></svg>',
    'setup-token': '<svg width=\"18\" height=\"18\" viewBox=\"0 0 16 16\" fill=\"currentColor\"><path d=\"M9.5 1a4.5 4.5 0 0 0-4.473 5.014l-4.74 4.74a.5.5 0 0 0-.146.353V13.5a.5.5 0 0 0 .5.5h2.5a.5.5 0 0 0 .5-.5V12h1.5a.5.5 0 0 0 .5-.5V10h1.793a.5.5 0 0 0 .353-.146l.547-.547A4.5 4.5 0 1 0 9.5 1zm-2 4.5a1 1 0 1 1 2 0 1 1 0 0 1-2 0z\"/></svg>',
  }};
  const container = document.getElementById('providers');
  const hint = document.getElementById('hint');
  const relayEl = document.getElementById('relay');
  function appendNext(url) {{
    if (!NEXT) return url;
    const sep = url.includes('?') ? '&' : '?';
    return url + sep + 'next=' + encodeURIComponent(NEXT);
  }}
  try {{
    // 探测当前是否已登录,显示当前用户 + Sign out
    try {{
      const u = await fetch('/auth/userinfo');
      if (u.ok) {{
        const info = await u.json();
        const box = document.getElementById('current-user');
        const text = document.getElementById('current-user-text');
        const name = info.name || info.sub || 'unknown';
        const provider = info.provider ? ' (' + info.provider + ')' : '';
        text.textContent = 'Signed in as ' + name + provider;
        box.style.display = '';
      }}
    }} catch (e) {{ /* ignore */ }}

    const resp = await fetch('/auth/providers');
    const data = await resp.json();
    const providers = data.providers || [];
    container.innerHTML = '';
    if (providers.length === 0) {{
      container.innerHTML = '<div class=\"empty\">No authentication providers configured.</div>';
      return;
    }}
    let hasSetupToken = false;
    for (const p of providers) {{
      const a = document.createElement('a');
      a.className = 'btn' + (p.type === 'setup-token' ? ' setup' : '');
      a.href = appendNext(p.url);
      const icon = ICONS[p.name] || ICONS.github;
      const label = p.type === 'setup-token'
        ? 'Sign in with Setup Token'
        : 'Sign in with ' + p.label;
      a.innerHTML = '<span>' + icon + '</span><span>' + label + '</span>';
      container.appendChild(a);
      if (p.type === 'setup-token') hasSetupToken = true;
    }}
    if (hasSetupToken) hint.style.display = '';
    if (data.relay_domain) {{
      relayEl.textContent = 'via ' + data.relay_domain;
      relayEl.style.display = '';
    }}
  }} catch (e) {{
    container.innerHTML = '<div class=\"empty\">Failed to load sign-in options.</div>';
  }}
}})();
</script>
</body>
</html>"""


class LoginPageView(View):
    """`/auth/login` — 独立登录页(纯 HTML)。

    - 任何用户都可以打开(已登录用户也能看到 — 提供"重新登录/切换身份"能力)
    - JS fetch /auth/providers 拿到 provider 列表 → 渲染按钮
    - 点击按钮跳转到 provider URL,带上 next 参数(让 callback 知道登录后回哪)
    """

    path = "/auth/login"

    async def get(self, request: Request) -> Response:
        next_url = _safe_next(request.query_params.get("next", ""))
        message = request.query_params.get("msg", "")
        # 只允许已知的消息类型(防止注入)
        if message not in ("", "logged_out", "session_expired"):
            message = ""
        message_text = {
            "logged_out": "You have been signed out.",
            "session_expired": "Your session has expired. Please sign in again.",
        }.get(message, "")
        return HTMLResponse(_render_login(next_url=next_url, message=message_text))


__all__ = ["LoginPageView"]
