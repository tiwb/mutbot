"""mutbot.copilot.provider -- GitHub Copilot LLM provider。

CopilotProvider 是 LLMProvider 的子类，通过 Copilot API 调用 LLM。
Copilot API 使用 OpenAI Chat Completions 格式，因此复用 OpenAIProvider 的格式转换逻辑。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, AsyncIterator

from mutagent.messages import (
    Message,
    StreamEvent,
    TextBlock,
    ToolSchema,
)
from mutagent.provider import LLMProvider
from mutagent.builtins.openai_provider import (
    _messages_to_openai,
    _tools_to_openai,
    _send_no_stream,
    _send_stream,
)
from mutbot.copilot.auth import CopilotAuth

logger = logging.getLogger(__name__)


class CopilotProvider(LLMProvider):
    """GitHub Copilot 后端 provider。

    通过 Copilot API（OpenAI 格式 + 专用 Headers）调用 LLM。

    Attributes:
        auth: CopilotAuth 实例（管理认证生命周期）。
        account_type: 账户类型（individual/business/enterprise）。
    """

    auth: CopilotAuth
    account_type: str = "individual"

    @classmethod
    def from_spec(cls, spec: dict) -> "CopilotProvider":
        github_token = spec.get("github_token")
        if not github_token:
            raise ValueError(
                "CopilotProvider requires 'github_token' in model spec.\n"
                "Run the setup wizard or add github_token to your config."
            )
        auth = CopilotAuth.get_instance()
        auth.github_token = github_token
        auth.ensure_authenticated()
        return cls(
            auth=auth,
            account_type=spec.get("account_type", "individual"),
        )

    async def send(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolSchema],
        prompts: list[Message] | None = None,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send messages to Copilot API and yield streaming events."""
        openai_messages = _messages_to_openai(messages)
        if prompts:
            for msg in reversed(prompts):
                for block in msg.blocks:
                    if isinstance(block, TextBlock) and block.text:
                        openai_messages.insert(0, {"role": "system", "content": block.text})

        payload: dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
        }
        if tools:
            payload["tools"] = _tools_to_openai(tools)

        # 获取 Copilot 专用 headers（含 JWT 认证）
        headers = self.auth.get_headers()

        # Copilot API base URL（按账户类型）
        base_url = self.auth.get_base_url(self.account_type)

        if stream:
            async for event in _send_stream(base_url, payload, headers):
                yield event
        else:
            async for event in _send_no_stream(base_url, payload, headers):
                yield event
