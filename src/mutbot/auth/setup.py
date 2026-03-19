"""mutbot.auth.setup — Auth 配置向导（relay 认证设置）。

通过 workspace 级 UI 引导用户配置 relay 认证。
向导流程：表单 → 存临时 nonce → 页面跳转 OAuth → relay-callback 保存配置。
"""

from __future__ import annotations

import asyncio
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


# ---------------------------------------------------------------------------
# 向导 async task
# ---------------------------------------------------------------------------


async def run_auth_setup_wizard(
    client: Any,
    context_id: str,
    self_origin: str,
) -> None:
    """Auth 配置向导后台 task。

    创建 client-bound UIContext，显示表单，收集信息后跳转 OAuth。
    """
    from mutbot.ui.context import UIContext
    from mutbot.ui.context_impl import register_context
    from mutbot.ui.events import UIEvent

    ui = UIContext(context_id=context_id, broadcast=_make_client_broadcast(client))
    register_context(ui)

    # 注册 client 断连回调
    def on_disconnect(_c: Any) -> None:
        queue = getattr(ui, '_event_queue', None)
        if queue is not None:
            queue.put_nowait(UIEvent(type="disconnect", data={}))

    client.on_disconnect(on_disconnect)

    try:
        result = await ui.show(_build_form_view())
        if result is None:
            return  # cancel 或 disconnect

        relay_url = (result.get("relay_url") or "https://mutbot.ai").strip().rstrip("/")
        access_mode = result.get("access_mode", "only_me")

        # 从 relay 获取支持的 provider 列表
        from mutbot.auth.views import _fetch_relay_providers, _create_nonce
        providers = await _fetch_relay_providers(relay_url)
        if not providers:
            # relay 不可达或无 provider
            await ui.show(_build_error_view("Cannot connect to relay server, or no providers available."))
            return
        # 使用第一个可用 provider（通常只有一个）
        provider = providers[0]

        # 创建 nonce 并存储临时状态
        nonce = _create_nonce()
        store_setup_nonce(nonce, relay_url, access_mode)

        # 构造登录 URL
        from urllib.parse import quote
        callback_url = f"{self_origin}/auth/relay-callback"
        login_url = (
            f"{relay_url}/auth/start"
            f"?callback={quote(callback_url)}"
            f"&provider={quote(provider)}"
            f"&nonce={nonce}"
        )

        # 关闭 UI 并通知前端跳转
        # 手动发送带 redirect 的 ui_close（而非 ui.close()），避免双发
        from mutbot.ui.context_impl import unregister_context
        from mutbot.web.rpc import make_event
        object.__setattr__(ui, '_closed', True)
        unregister_context(context_id)
        client.enqueue("json", make_event("ui_close", {
            "context_id": context_id,
            "redirect": login_url,
        }))

    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Auth setup wizard error")
    finally:
        ui.close()  # 幂等


def save_auth_config(relay_url: str, access_mode: str, user_sub: str) -> None:
    """保存 auth 配置到 ~/.mutbot/config.json（合并，保留已有的 relay_service 等）。"""
    from mutbot.web import server as _server_mod
    config = _server_mod.config
    if config is None:
        return

    # 用点分路径写入，不覆盖 auth 下的其他 key（如 relay_service）
    config.set("auth.relay", relay_url)
    if access_mode == "only_me":
        config.set("auth.allowed_users", [user_sub])

    logger.info("Auth config saved: relay=%s, mode=%s, user=%s", relay_url, access_mode, user_sub)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _make_client_broadcast(client: Any):
    """创建绑定到单个 client 的 broadcast 函数。

    将 UIContext 的消息（ui_view/ui_close）包装成 workspace event 格式，
    使前端 wsRpc.on() 能正确接收。
    """
    from mutbot.web.rpc import make_event

    def broadcast(data: dict) -> None:
        msg_type = data.get("type", "")
        if msg_type in ("ui_view", "ui_close"):
            # 包装为 event 格式：{type: "event", event: "ui_view", data: {...}}
            event_data = {k: v for k, v in data.items() if k != "type"}
            client.enqueue("json", make_event(msg_type, event_data))
        else:
            client.enqueue("json", data)
    return broadcast


def _build_form_view() -> dict:
    """构建向导表单 View Schema。"""
    return {
        "title": "Auth Setup",
        "components": [
            {
                "type": "hint",
                "id": "intro",
                "text": "Enable login to control who can access your MutBot server.",
            },
            {
                "type": "text",
                "id": "relay_url",
                "label": "Relay Server",
                "value": "https://mutbot.ai",
                "placeholder": "https://mutbot.ai",
            },
            {
                "type": "hint",
                "id": "relay_hint",
                "text": "Uses a relay server for zero-config GitHub login. No registration needed.",
            },
            {
                "type": "select",
                "id": "access_mode",
                "label": "Access Mode",
                "value": "only_me",
                "layout": "vertical",
                "options": [
                    {"value": "anyone", "label": "Anyone can log in"},
                    {"value": "only_me", "label": "Only me (first login becomes admin)"},
                ],
            },
        ],
        "actions": [
            {"type": "cancel", "label": "Cancel"},
            {"type": "submit", "label": "Sign in →", "primary": True},
        ],
    }


def _build_error_view(message: str) -> dict:
    """构建错误提示 View。"""
    return {
        "title": "Auth Setup",
        "components": [
            {"type": "hint", "id": "error", "text": f"**Error:** {message}"},
        ],
        "actions": [
            {"type": "cancel", "label": "Close"},
        ],
    }
