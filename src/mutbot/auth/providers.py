"""mutbot.auth.providers — OIDC Provider 抽象 + GitHub 预设 + 通用 OIDC。

每个 Provider 封装 Authorization Code Flow 的三步：
1. authorize_url() — 生成授权跳转 URL
2. exchange_code() — 用 code 换取 access_token
3. get_userinfo() — 获取用户信息
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


@dataclass
class UserInfo:
    """标准化的用户信息。"""
    sub: str        # provider:username
    name: str       # 显示名
    avatar: str     # 头像 URL
    provider: str   # 提供商名称


class OIDCProvider:
    """OIDC Provider 基类。"""

    def __init__(
        self,
        name: str,
        *,
        client_id: str,
        client_secret: str,
        authorization_endpoint: str,
        token_endpoint: str,
        userinfo_endpoint: str,
        scopes: list[str] | None = None,
    ) -> None:
        self.name = name
        self.client_id = client_id
        self.client_secret = client_secret
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self.userinfo_endpoint = userinfo_endpoint
        self.scopes = scopes or ["openid", "profile"]

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        """生成授权跳转 URL。"""
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
        }
        return f"{self.authorization_endpoint}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        """用 authorization code 换取 access_token。"""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            data = resp.json()
            if "error" in data:
                raise ValueError(f"Token exchange failed: {data}")
            return data["access_token"]

    async def get_userinfo(self, access_token: str) -> UserInfo:
        """获取用户信息。子类可覆盖以适配不同 Provider 的响应格式。"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.userinfo_endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
            data = resp.json()
            return self._parse_userinfo(data)

    def _parse_userinfo(self, data: dict[str, Any]) -> UserInfo:
        """解析 userinfo 响应。子类覆盖以适配不同格式。"""
        username = data.get("preferred_username") or data.get("sub", "unknown")
        return UserInfo(
            sub=f"{self.name}:{username}",
            name=data.get("name") or username,
            avatar=data.get("picture", ""),
            provider=self.name,
        )


class GitHubProvider(OIDCProvider):
    """GitHub OAuth App 预设。端点硬编码（GitHub 不支持 OIDC discovery）。"""

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        super().__init__(
            "github",
            client_id=client_id,
            client_secret=client_secret,
            authorization_endpoint="https://github.com/login/oauth/authorize",
            token_endpoint="https://github.com/login/oauth/access_token",
            userinfo_endpoint="https://api.github.com/user",
            scopes=["read:user"],
        )

    async def exchange_code(self, code: str, redirect_uri: str) -> str:
        """GitHub 用 JSON POST 换 token。"""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.token_endpoint,
                json={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            data = resp.json()
            if "error" in data:
                raise ValueError(f"GitHub token exchange failed: {data}")
            return data["access_token"]

    async def get_userinfo(self, access_token: str) -> UserInfo:
        """GitHub 用户 API 响应格式不同于标准 OIDC。"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.userinfo_endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "User-Agent": "MutBot-Auth",
                },
            )
            data = resp.json()
            return UserInfo(
                sub=f"github:{data['login']}",
                name=data.get("name") or data["login"],
                avatar=data.get("avatar_url", ""),
                provider="github",
            )


# ---------------------------------------------------------------------------
# Provider 工厂
# ---------------------------------------------------------------------------


async def create_provider_from_issuer(
    name: str,
    *,
    issuer: str,
    client_id: str,
    client_secret: str,
    scopes: list[str] | None = None,
) -> OIDCProvider:
    """从 issuer URL 自动发现端点（OIDC Discovery）。"""
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient() as client:
        resp = await client.get(discovery_url)
        config = resp.json()
    return OIDCProvider(
        name,
        client_id=client_id,
        client_secret=client_secret,
        authorization_endpoint=config["authorization_endpoint"],
        token_endpoint=config["token_endpoint"],
        userinfo_endpoint=config["userinfo_endpoint"],
        scopes=scopes,
    )


def create_provider_from_config(name: str, cfg: dict[str, Any]) -> OIDCProvider:
    """从配置 dict 创建 Provider。

    支持三种形式：
    1. GitHub 预设：name="github"，只需 client_id + client_secret
    2. OIDC issuer：提供 issuer URL（同步创建，端点需 discovery，返回占位）
    3. 手动端点：提供 authorization_endpoint + token_endpoint + userinfo_endpoint
    """
    client_id = cfg["client_id"]
    client_secret = cfg["client_secret"]

    if name == "github":
        return GitHubProvider(client_id=client_id, client_secret=client_secret)

    # 手动端点
    if "authorization_endpoint" in cfg:
        return OIDCProvider(
            name,
            client_id=client_id,
            client_secret=client_secret,
            authorization_endpoint=cfg["authorization_endpoint"],
            token_endpoint=cfg["token_endpoint"],
            userinfo_endpoint=cfg["userinfo_endpoint"],
            scopes=cfg.get("scopes"),
        )

    # issuer 模式 — 需要异步 discovery，这里返回占位，启动时异步初始化
    raise ValueError(
        f"Provider '{name}' 需要 authorization_endpoint 或 issuer。"
        " issuer 模式请使用 create_provider_from_issuer()。"
    )


def generate_state(nonce: str | None = None) -> str:
    """生成 OAuth state 参数。可选包含 nonce。"""
    return nonce or secrets.token_urlsafe(32)
