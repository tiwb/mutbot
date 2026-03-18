"""mutbot.auth.relay — 中转服务端路由（让本 mutbot 实例作为中转站）。

RelayStartView              /auth/start              — 发起 OAuth
RelayProviderCallbackView   /auth/relay/callback     — Provider 回调，签发断言
RelayMetaView               /.well-known/mutbot-relay.json — 中转站元信息
"""

from __future__ import annotations

import logging
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any
from urllib.parse import urlencode

import jwt
from mutagent.net.server import View, Request, Response, json_response

from mutbot.auth.providers import create_provider_from_config

logger = logging.getLogger(__name__)


def _get_relay_service_config() -> dict[str, Any] | None:
    """获取 auth.relay_service 配置。"""
    from mutbot.web import server as _server_mod
    cfg = _server_mod.config
    if cfg is None:
        return None
    return cfg.get("auth.relay_service")


def _get_private_key_pem(relay_cfg: dict[str, Any]) -> str | None:
    """从配置获取 Ed25519 私钥 PEM。"""
    return relay_cfg.get("private_key")


def _get_public_key_pem_from_private(private_key_pem: str) -> str:
    """从私钥导出公钥 PEM。"""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_private_key,
    )
    private_key = load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    public_key = private_key.public_key()
    return public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode("utf-8")


def _encode_state(data: dict[str, str]) -> str:
    """将 state 数据编码为 URL 安全字符串。"""
    import json as _json
    return urlsafe_b64encode(_json.dumps(data).encode()).decode().rstrip("=")


def _decode_state(state: str) -> dict[str, str]:
    """解码 state 字符串。"""
    import json as _json
    # 补齐 base64 padding
    padding = 4 - len(state) % 4
    if padding != 4:
        state += "=" * padding
    return _json.loads(urlsafe_b64decode(state).decode())


# ---------------------------------------------------------------------------
# RelayStartView — 发起 OAuth
# ---------------------------------------------------------------------------


class RelayStartView(View):
    """/auth/start — 中转站：发起 OAuth 认证。"""
    path = "/auth/start"

    async def get(self, request: Request) -> Response:
        relay_cfg = _get_relay_service_config()
        if not relay_cfg:
            return json_response({"error": "relay service not configured"}, status=404)

        callback = request.query_params.get("callback", "")
        provider_name = request.query_params.get("provider", "github")
        nonce = request.query_params.get("nonce", "")

        if not callback or not nonce:
            return json_response({"error": "missing callback or nonce"}, status=400)

        # 校验 callback URL：必须是 /auth/relay-callback 路径
        from urllib.parse import urlparse
        parsed_cb = urlparse(callback)
        if parsed_cb.path.rstrip("/") != "/auth/relay-callback":
            return json_response({"error": "invalid callback path"}, status=400)

        # 获取对应 Provider
        providers_cfg = relay_cfg.get("providers", {})
        if provider_name not in providers_cfg:
            return json_response({"error": f"unsupported provider: {provider_name}"}, status=400)

        provider = create_provider_from_config(provider_name, providers_cfg[provider_name])

        # state 编码回调信息
        state = _encode_state({
            "callback": callback,
            "nonce": nonce,
            "provider": provider_name,
        })

        redirect_uri = _get_callback_url_for_relay(request)
        url = provider.authorize_url(redirect_uri, state)
        return Response(status=302, headers={"location": url})


def _get_callback_url_for_relay(request: Request) -> str:
    """构建中转站自身的 Provider 回调 URL。"""
    host = request.headers.get("host", "localhost:8741")
    proto = request.headers.get("x-forwarded-proto", "http")
    return f"{proto}://{host}/auth/relay/callback"


# ---------------------------------------------------------------------------
# RelayProviderCallbackView — Provider 回调，签发断言
# ---------------------------------------------------------------------------


class RelayProviderCallbackView(View):
    """/auth/relay/callback — 中转站：Provider 回调，签发断言 JWT。"""
    path = "/auth/relay/callback"

    async def get(self, request: Request) -> Response:
        relay_cfg = _get_relay_service_config()
        if not relay_cfg:
            return json_response({"error": "relay service not configured"}, status=404)

        code = request.query_params.get("code", "")
        state_str = request.query_params.get("state", "")
        if not code or not state_str:
            return json_response({"error": "missing code or state"}, status=400)

        try:
            state = _decode_state(state_str)
        except Exception:
            return json_response({"error": "invalid state"}, status=400)

        callback = state.get("callback", "")
        nonce = state.get("nonce", "")
        provider_name = state.get("provider", "")

        # 获取 Provider
        providers_cfg = relay_cfg.get("providers", {})
        if provider_name not in providers_cfg:
            return json_response({"error": f"unknown provider: {provider_name}"}, status=400)

        provider = create_provider_from_config(provider_name, providers_cfg[provider_name])

        try:
            redirect_uri = _get_callback_url_for_relay(request)
            access_token = await provider.exchange_code(code, redirect_uri)
            userinfo = await provider.get_userinfo(access_token)
        except Exception as e:
            logger.error("中转 Provider 回调失败: %s", e)
            return json_response({"error": "authentication failed"}, status=500)

        # 签发断言 JWT（Ed25519）
        private_key_pem = _get_private_key_pem(relay_cfg)
        if not private_key_pem:
            return json_response({"error": "relay private key not configured"}, status=500)

        now = int(time.time())
        assertion_payload = {
            "sub": userinfo.sub,
            "name": userinfo.name,
            "avatar": userinfo.avatar,
            "provider": userinfo.provider,
            "nonce": nonce,
            "aud": callback,
            "iat": now,
            "exp": now + 300,  # 5 分钟
        }

        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            private_key = load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
            assertion = jwt.encode(assertion_payload, private_key, algorithm="EdDSA")
        except Exception as e:
            logger.error("签发断言失败: %s", e)
            return json_response({"error": "assertion signing failed"}, status=500)

        # 重定向回 mutbot 实例，assertion 在 URL fragment 中
        from urllib.parse import urlparse
        parsed = urlparse(callback)
        redirect_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}#assertion={assertion}"
        return Response(status=302, headers={"location": redirect_url})


# ---------------------------------------------------------------------------
# RelayMetaView — 中转站元信息
# ---------------------------------------------------------------------------


class RelayMetaView(View):
    """/.well-known/mutbot-relay.json — 中转站元信息。"""
    path = "/.well-known/mutbot-relay.json"

    async def get(self, request: Request) -> Response:
        relay_cfg = _get_relay_service_config()
        if not relay_cfg:
            return json_response({"error": "relay service not configured"}, status=404)

        # 导出公钥
        private_key_pem = _get_private_key_pem(relay_cfg)
        public_key_pem = ""
        if private_key_pem:
            try:
                public_key_pem = _get_public_key_pem_from_private(private_key_pem)
            except Exception as e:
                logger.error("导出公钥失败: %s", e)

        # 支持的 Provider 列表
        providers = list(relay_cfg.get("providers", {}).keys())

        return json_response({
            "name": "MutBot Auth Relay",
            "version": 1,
            "providers": providers,
            "verify": "ed25519",
            "public_key": public_key_pem,
        })
