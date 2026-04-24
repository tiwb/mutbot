"""mutbot.auth.setup_view — Auth setup wizard (mutgui-based).

替代旧 `auth/views.py:_render_setup_page` 的 970 行 HTML 模板。

- `AuthSetupView` (mutgui.View)：单一 View，按 step 状态渲染不同 UI。
- `AuthSetupWsView` (WebSocketView)：每个 WS 连接独立创建一个 `AuthSetupView`
  实例 + `ViewPort`，经由 `MutguiChannel` 发送 wire tree。

HTTP 入口（30 行 HTML 壳，挂载 setup.js）保留在 `auth/views.py` 中。
"""

from __future__ import annotations

import json
import logging
from functools import partial
from typing import Any
from urllib.parse import quote

from mutgui import Bind, Callback, Channel, View, ViewBlock, ViewPort

from mutio.net.server import WebSocketConnection, WebSocketDisconnect, WebSocketView

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channel 适配 — mutio.net WebSocket
# ---------------------------------------------------------------------------


class MutbotMutguiChannel(Channel):
    """把 mutgui 的 send(message: dict) 接到 mutio.net 的 WebSocketConnection。"""

    def __init__(self, ws: WebSocketConnection) -> None:
        super().__init__()
        self.ws = ws

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws.send_json(message)


# ---------------------------------------------------------------------------
# AuthSetupView — 状态机 + render
# ---------------------------------------------------------------------------


def _humanize(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").title()


def _read_current_relay() -> str:
    from mutbot.web import server as _server_mod
    cfg = _server_mod.config
    if cfg is None:
        return ""
    auth = cfg.get("auth") or {}
    return auth.get("relay", "") or ""


def _is_already_configured() -> bool:
    from mutbot.web import server as _server_mod
    cfg = _server_mod.config
    if cfg is None:
        return False
    auth = cfg.get("auth") or {}
    return bool(auth.get("relay") or auth.get("providers"))


def _print_setup_token_console(token: str) -> None:
    """重新激活 setup token 时在服务端控制台打印（reconfigure 远程准入用）。"""
    print()
    print("  WARNING: Reconfigure requested. Setup token re-issued:")
    print()
    print(f"      Setup Token: {token}")
    print()


class AuthSetupView(View):
    """Auth setup wizard 单 View 状态机。

    step 取值：
      - "token_input"        — 远程访问需输入 setup token
      - "configure"          — 输入 relay URL
      - "select_provider"    — 从 relay 拉到 provider 列表，选 provider
      - "already_configured" — 已配置，提供 Reconfigure 入口
      - "redirecting"        — 通过 mutbot.Redirect 让前端 location.href 跳到 OAuth
    """

    def __init__(self, *, is_local: bool) -> None:
        super().__init__()
        self.is_local = is_local

        if _is_already_configured():
            self.step = "already_configured"
        else:
            import mutbot.auth.setup_token as setup_token
            if not is_local and setup_token.is_active():
                self.step = "token_input"
            else:
                self.step = "configure"

        self.error: str = ""
        self.relay_url: str = "https://mutbot.ai"
        self.providers: list[dict[str, str]] = []
        self.token_input: str = ""
        # 远程 token 验证后置真，控制 reconfigure → 进入 configure 时是否清空配置
        self.setup_verified: bool = False
        # token_input 验证完成后是否执行 reconfigure（清空配置）
        self.pending_reconfigure: bool = False
        # 需要前端跳转的 URL（step == "redirecting" 时使用）
        self.redirect_url: str = ""

    # ---- render ----

    def render(self) -> ViewBlock:
        step = self.step
        if step == "already_configured":
            return self._render_already_configured()
        if step == "token_input":
            return self._render_token_input()
        if step == "configure":
            return self._render_configure()
        if step == "select_provider":
            return self._render_select_provider()
        if step == "redirecting":
            return self._render_redirecting()
        return ViewBlock([
            {"$component": "antd.Alert", "$id": "err",
             "type": "error", "message": f"Unknown step: {step}"},
        ])

    # ---- 各 step ----

    def _container(self, *, title: str, subtitle: str, children: list[dict[str, Any]]) -> ViewBlock:
        items: list[dict[str, Any]] = [
            {"$component": "antd.Typography.Title", "$id": "title",
             "level": 3, "style": {"marginTop": 0}, "children": title},
            {"$component": "antd.Typography.Paragraph", "$id": "subtitle",
             "type": "secondary", "children": subtitle},
        ]
        if self.error:
            items.append({
                "$component": "antd.Alert", "$id": "error",
                "type": "error", "showIcon": True, "message": self.error,
                "style": {"marginBottom": 16},
            })
        items.extend(children)
        return ViewBlock([
            {"$component": "div", "$id": "wrap",
             "style": {"maxWidth": 460, "margin": "40px auto", "padding": "32px",
                       "background": "#1f1f1f", "border": "1px solid #303030",
                       "borderRadius": 12, "color": "#d4d4d4"},
             "$children": items},
        ])

    def _render_already_configured(self) -> ViewBlock:
        relay = _read_current_relay()
        return self._container(
            title="Authentication Configured",
            subtitle=(
                f"Auth is set up via {relay}." if relay
                else "Auth is already configured on this server."
            ),
            children=[
                {"$component": "antd.Space", "$id": "actions",
                 "size": 12, "style": {"marginTop": 8},
                 "$children": [
                     {"$component": "antd.Button", "$id": "back",
                      "type": "primary", "children": "Back to MutBot",
                      "onClick": Callback(self._on_back_home)},
                     {"$component": "antd.Button", "$id": "reconfigure",
                      "danger": True, "children": "Reconfigure",
                      "onClick": Callback(self._on_reconfigure)},
                 ]},
            ],
        )

    def _render_token_input(self) -> ViewBlock:
        return self._container(
            title="Setup Token Required",
            subtitle=(
                "This server has no authentication configured. "
                "Enter the setup token printed on the server console to continue."
            ),
            children=[
                {"$component": "antd.Input", "$id": "token",
                 "value": self.token_input,
                 "placeholder": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                 "autoFocus": True,
                 "style": {"fontFamily": "monospace", "marginBottom": 16},
                 "onChange": Bind(self, "token_input", "$0.target.value"),
                 "onPressEnter": Callback(self._on_verify_token)},
                {"$component": "antd.Button", "$id": "verify",
                 "type": "primary", "block": True, "children": "Verify",
                 "onClick": Callback(self._on_verify_token)},
            ],
        )

    def _render_configure(self) -> ViewBlock:
        children: list[dict[str, Any]] = [
            {"$component": "antd.Typography.Text", "$id": "lbl",
             "type": "secondary", "children": "Relay Server"},
            {"$component": "antd.Input", "$id": "relay",
             "value": self.relay_url,
             "placeholder": "https://mutbot.ai",
             "style": {"marginTop": 6, "marginBottom": 8,
                       "fontFamily": "monospace"},
             "onChange": Bind(self, "relay_url", "$0.target.value"),
             "onPressEnter": Callback(self._on_connect_relay)},
            {"$component": "antd.Typography.Paragraph", "$id": "hint",
             "type": "secondary",
             "style": {"fontSize": 12, "marginBottom": 16},
             "children": (
                 "Uses a relay server for zero-config login. "
                 "No registration needed."
             )},
            {"$component": "antd.Button", "$id": "connect",
             "type": "primary", "block": True, "children": "Connect →",
             "onClick": Callback(self._on_connect_relay)},
        ]
        if _is_already_configured():
            children.append({
                "$component": "antd.Button", "$id": "cancel",
                "type": "link", "style": {"marginTop": 8},
                "children": "← Cancel (keep current config)",
                "onClick": Callback(self._on_back_to_configured),
            })
        return self._container(
            title="Configure Authentication",
            subtitle="Set up login to control who can access this MutBot server.",
            children=children,
        )

    def _render_select_provider(self) -> ViewBlock:
        buttons: list[dict[str, Any]] = []
        for i, p in enumerate(self.providers):
            label = p["label"]
            buttons.append({
                "$component": "antd.Button", "$id": f"prov-{i}",
                "type": "primary", "block": True,
                "size": "large",
                "style": {"marginBottom": 8},
                "children": f"Sign in with {label} →",
                "onClick": Callback(partial(self._on_start_oauth, p["name"])),
            })

        return self._container(
            title="Choose Login Provider",
            subtitle=f"via {self.relay_url}",
            children=[
                {"$component": "div", "$id": "list", "$children": buttons},
                {"$component": "antd.Button", "$id": "back",
                 "type": "link", "style": {"marginTop": 8},
                 "children": "← Back",
                 "onClick": Callback(self._on_back_to_configure)},
            ],
        )

    def _render_redirecting(self) -> ViewBlock:
        return ViewBlock([
            {"$component": "mutbot.Redirect", "$id": "redir",
             "url": self.redirect_url},
        ])

    # ---- 回调 ----

    def _on_back_home(self) -> None:
        self.redirect_url = "/"
        self.step = "redirecting"
        self.invalidate()

    async def _on_reconfigure(self) -> None:
        """Reconfigure 入口准入控制：本地直接进入 configure；远程触发 token 重新激活。

        不在此处清空旧配置 —— 新配置会在 OAuth 回调成功时由 save_auth_config 覆盖。
        预先清空会导致用户中途退出（刷新/关闭）后旧配置丢失。
        """
        if self.is_local:
            self.step = "configure"
            self.error = ""
            self.relay_url = _read_current_relay() or "https://mutbot.ai"
            self.invalidate()
            return

        # 远程：重新激活 setup token，控制台打印新 token，进入 token_input
        import mutbot.auth.setup_token as setup_token
        token = setup_token.generate()
        _print_setup_token_console(token)
        self.pending_reconfigure = True
        self.step = "token_input"
        self.error = ""
        self.token_input = ""
        self.setup_verified = False
        self.invalidate()

    async def _on_verify_token(self) -> None:
        import mutbot.auth.setup_token as setup_token
        token = (self.token_input or "").strip()
        if not token:
            self.error = "Please enter the setup token"
            self.invalidate()
            return
        if not setup_token.verify(token):
            self.error = "Invalid token. Check the server console output."
            self.invalidate()
            return

        self.setup_verified = True
        self.error = ""
        self.token_input = ""
        self.pending_reconfigure = False

        self.step = "configure"
        self.relay_url = _read_current_relay() or "https://mutbot.ai"
        self.invalidate()

    async def _on_connect_relay(self) -> None:
        relay_url = (self.relay_url or "").strip().rstrip("/")
        if not relay_url:
            self.error = "Please enter a relay URL"
            self.invalidate()
            return

        # 远程未通过 token 验证（且 setup token 仍处于 active）→ 退回 token_input
        import mutbot.auth.setup_token as setup_token
        if (not self.is_local) and setup_token.is_active() and not self.setup_verified:
            self.step = "token_input"
            self.error = "Token expired or invalid. Please re-enter."
            self.invalidate()
            return

        from mutbot.auth.views import _validate_relay_url, _fetch_relay_providers

        ssrf_error = _validate_relay_url(relay_url)
        if ssrf_error:
            self.error = ssrf_error
            self.invalidate()
            return

        provider_names = await _fetch_relay_providers(relay_url)
        if not provider_names:
            self.error = f"Cannot connect to relay server: {relay_url}"
            self.invalidate()
            return

        self.relay_url = relay_url
        self.providers = [
            {"name": n, "label": _humanize(n)} for n in provider_names
        ]
        self.error = ""
        self.step = "select_provider"
        self.invalidate()

    def _on_back_to_configure(self) -> None:
        self.step = "configure"
        self.error = ""
        self.invalidate()

    def _on_back_to_configured(self) -> None:
        """从 configure 退回 already_configured（前提：仍处于已配置状态）。"""
        self.step = "already_configured"
        self.error = ""
        self.invalidate()

    async def _on_start_oauth(self, provider: str) -> None:
        if not provider:
            self.error = "No provider selected"
            self.invalidate()
            return

        # 远程未通过 token 验证 → 退回 token_input
        import mutbot.auth.setup_token as setup_token
        if (not self.is_local) and setup_token.is_active() and not self.setup_verified:
            self.step = "token_input"
            self.error = "Token expired or invalid. Please re-enter."
            self.invalidate()
            return

        from mutbot.auth.views import _create_nonce
        from mutbot.auth.setup import store_setup_nonce

        nonce = _create_nonce()
        store_setup_nonce(nonce, self.relay_url, "only_me")

        # 回调 URL 取自当前 server 配置（base_path）
        callback_url = self._build_callback_url("/auth/relay-callback")
        login_url = (
            f"{self.relay_url}/auth/start"
            f"?callback={quote(callback_url)}"
            f"&provider={quote(provider)}"
            f"&nonce={nonce}"
        )

        self.redirect_url = login_url
        self.step = "redirecting"
        self.invalidate()

    # ---- 工具 ----

    def _build_callback_url(self, path: str) -> str:
        from mutbot.web import server as _server_mod
        base_path = ""
        cfg = _server_mod.config
        if cfg is not None:
            base_path = cfg.get("base_path", default="") or ""
        # WebSocket 上下文下没有原始 HTTP request，scheme/host 从连接 headers 取
        host = self._ws_host or "localhost:8741"
        scheme = "https" if self._ws_secure else "http"
        return f"{scheme}://{host}{base_path}{path}"

    @staticmethod
    def _clear_auth_config() -> None:
        """清空 auth 配置（保留 auth 下的其他 key 如 relay_service）。"""
        from mutbot.web import server as _server_mod
        cfg = _server_mod.config
        if cfg is None:
            return
        auth = cfg.get("auth") or {}
        for key in ("relay", "providers", "allowed_users"):
            if key in auth:
                cfg.set(f"auth.{key}", None)
        logger.info("Auth config cleared (reconfigure)")

    # 由 AuthSetupWsView 在创建实例后注入（用于构造 callback URL）
    _ws_host: str = ""
    _ws_secure: bool = False


# ---------------------------------------------------------------------------
# AuthSetupWsView — WebSocket 入口
# ---------------------------------------------------------------------------


class AuthSetupWsView(WebSocketView):
    """每个 WS 连接独立创建一个 AuthSetupView 实例。"""

    path = "/auth/setup/ws"

    async def connect(self, ws: WebSocketConnection) -> None:
        await ws.accept()

        # 从中间件 ContextVar 读取 client IP，决定是否本地
        from mutbot.auth.middleware import current_client_ip
        from mutbot.auth.network import is_loopback_ip
        client_ip = current_client_ip.get()
        is_local = is_loopback_ip(client_ip) if client_ip else True

        view = AuthSetupView(is_local=is_local)
        # 把 host / scheme 透传给 view（用于构造 OAuth callback URL）
        view._ws_host = ws.headers.get("host", "")
        view._ws_secure = ws.headers.get("x-forwarded-proto", "") == "https"

        channel = MutbotMutguiChannel(ws)
        viewport = ViewPort(view, channel)
        await viewport.initialize()
        await view.rendered()

        try:
            while True:
                raw = await ws.receive()
                text = raw.get("text")
                if text is None:
                    continue
                try:
                    event = json.loads(text)
                except Exception:
                    logger.debug("setup ws: invalid json")
                    continue
                await viewport.handle_event(event)
        except WebSocketDisconnect:
            logger.debug("setup ws disconnected")
        except Exception as exc:
            logger.warning("setup ws error: %s", exc)
        finally:
            viewport.detach()


__all__ = ["AuthSetupView", "AuthSetupWsView", "MutbotMutguiChannel"]
