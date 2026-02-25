"""mutbot.copilot.provider -- GitHub Copilot LLM provider。

CopilotProvider 是 LLMProvider 的子类，通过 Copilot API 调用 LLM。
Copilot API 使用 OpenAI Chat Completions 格式，因此复用 OpenAIProvider 的格式转换逻辑。
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import requests

from mutagent.messages import (
    Message,
    StreamEvent,
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
    def from_config(cls, config: dict) -> "CopilotProvider":
        auth = CopilotAuth.get_instance()
        auth.ensure_authenticated()
        return cls(
            auth=auth,
            account_type=config.get("account_type", "individual"),
        )

    def send(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolSchema],
        system_prompt: str = "",
        stream: bool = True,
    ) -> Iterator[StreamEvent]:
        """Send messages to Copilot API and yield streaming events."""
        openai_messages = _messages_to_openai(messages)
        if system_prompt:
            openai_messages.insert(0, {"role": "system", "content": system_prompt})

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
            yield from _send_stream(base_url, payload, headers)
        else:
            yield from _send_no_stream(base_url, payload, headers)
