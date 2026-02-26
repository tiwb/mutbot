"""mutbot.copilot.auth -- GitHub Copilot 认证管理。

认证流程：
1. GitHub token 由配置文件（~/.mutbot/config.json）中的 github_token 字段提供
2. GitHub token 换取 Copilot JWT（仅内存，过期时自动刷新）
3. 首次认证通过 setup wizard 的 OAuth 设备流完成
"""

from __future__ import annotations

import logging
import sys
import time

import requests

logger = logging.getLogger(__name__)

# VS Code Copilot Chat 使用的 Client ID
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"

# Copilot API base URLs（按账户类型）
COPILOT_BASE_URLS = {
    "individual": "https://api.githubcopilot.com",
    "business": "https://api.business.githubcopilot.com",
    "enterprise": "https://api.enterprise.githubcopilot.com",
}

# VS Code 版本号（用于 headers）
VSCODE_VERSION = "1.99.0"


class CopilotAuth:
    """GitHub Copilot 认证管理（单例）。

    管理两层 token：
    - github_token: 由调用方（配置或向导）设置
    - copilot_token: 通过 github_token 换取的 JWT，仅在内存中
    """

    _instance: CopilotAuth | None = None

    def __init__(self) -> None:
        self.github_token: str | None = None
        self.copilot_token: str | None = None
        self.expires_at: float = 0.0

    @classmethod
    def get_instance(cls) -> CopilotAuth:
        """获取单例实例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_token(self) -> str:
        """获取有效的 Copilot JWT。过期时同步刷新。

        Returns:
            Copilot JWT token。

        Raises:
            RuntimeError: 未认证或刷新失败。
        """
        if not self.github_token:
            raise RuntimeError(
                "Not authenticated. Run `python -m mutbot` to start the setup wizard."
            )
        if self._is_expired():
            self._refresh_copilot_token()
        assert self.copilot_token is not None
        return self.copilot_token

    def ensure_authenticated(self) -> None:
        """确保已认证。未认证时触发 GitHub 设备流。"""
        if not self.github_token:
            self._device_flow()
        self._refresh_copilot_token()

    def get_base_url(self, account_type: str = "individual") -> str:
        """获取 Copilot API base URL。"""
        return COPILOT_BASE_URLS.get(account_type, COPILOT_BASE_URLS["individual"])

    def get_headers(self) -> dict[str, str]:
        """获取 Copilot API 请求所需的 headers。"""
        from uuid import uuid4

        token = self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "copilot-integration-id": "vscode-chat",
            "editor-version": f"vscode/{VSCODE_VERSION}",
            "editor-plugin-version": "copilot-chat/0.26.7",
            "user-agent": "GitHubCopilotChat/0.26.7",
            "openai-intent": "conversation-panel",
            "x-github-api-version": "2025-04-01",
            "x-request-id": str(uuid4()),
        }

    def _is_expired(self) -> bool:
        """检查 Copilot JWT 是否已过期（提前 5 分钟）。"""
        return self.copilot_token is None or time.time() >= (self.expires_at - 300)

    def _device_flow(self) -> None:
        """GitHub OAuth 设备流。

        交互式流程：
        1. 请求设备码
        2. 用户在浏览器中输入设备码
        3. 轮询获取 access token
        """
        logger.info("Starting GitHub OAuth device flow")

        # 1. 请求设备码
        resp = requests.post(
            "https://github.com/login/device/code",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "scope": "read:user",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        device_code = data["device_code"]
        user_code = data["user_code"]
        verification_uri = data["verification_uri"]
        interval = data.get("interval", 5)

        print(f"\n  Open: {verification_uri}")
        print(f"  Enter code: {user_code}\n")

        # 2. 轮询获取 access token
        while True:
            time.sleep(interval)
            resp = requests.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            data = resp.json()

            error = data.get("error")
            if error == "authorization_pending":
                continue
            elif error == "slow_down":
                interval += 5
                continue
            elif error == "expired_token":
                raise RuntimeError("Device code expired. Please try again.")
            elif error == "access_denied":
                raise RuntimeError("Authorization denied by user.")
            elif error:
                raise RuntimeError(f"OAuth error: {error}")

            # 成功
            self.github_token = data["access_token"]
            logger.info("GitHub authentication successful")
            return

    def _refresh_copilot_token(self) -> None:
        """通过 GitHub token 换取 Copilot JWT。"""
        if not self.github_token:
            raise RuntimeError("No GitHub token available")

        resp = requests.get(
            "https://api.github.com/copilot_internal/v2/token",
            headers={
                "Authorization": f"token {self.github_token}",
                "Accept": "application/json",
                "User-Agent": "GitHubCopilotChat/0.26.7",
            },
        )

        if resp.status_code == 401:
            self.github_token = None
            self.copilot_token = None
            raise RuntimeError(
                "GitHub token expired. Update github_token in ~/.mutbot/config.json "
                "or re-run the setup wizard."
            )

        resp.raise_for_status()
        data = resp.json()

        self.copilot_token = data["token"]
        self.expires_at = data["expires_at"]
        logger.info("Copilot JWT refreshed (expires_at=%.0f)", self.expires_at)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    auth = CopilotAuth()
    try:
        auth.ensure_authenticated()
        print(f"Authentication successful! Token: {auth.github_token}")
        print("Add this token as 'github_token' in your ~/.mutbot/config.json")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
