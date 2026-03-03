"""向导 Agent Session — 用户首先接触的入口。

MutBot 的默认会话类型，具备 Web 搜索能力，可直接回答用户问题，
也可以创建专业 Agent Session 来处理特定任务。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, TYPE_CHECKING
from uuid import uuid4

from mutagent.agent import Agent
from mutagent.context import AgentContext
from mutagent.messages import Message, Response, StreamEvent, TextBlock, ToolUseBlock
from mutagent.provider import LLMProvider
from mutagent.tools import ToolSet

from mutbot.session import AgentSession

if TYPE_CHECKING:
    from mutagent.config import Config


# ---------------------------------------------------------------------------
# NullProvider — 无 LLM 配置时占位
# ---------------------------------------------------------------------------

class NullProvider(LLMProvider):
    """占位 LLM Provider — 无 LLM 配置时满足 Agent 构造要求。

    无论用户发什么消息，都返回引导文本 + Setup-llm tool_use，
    让 Agent 自动进入配置流程。
    配置完成后由 SetupToolkit._activate() 直接替换 agent.llm。
    """

    @classmethod
    def from_config(cls, model_config: dict) -> NullProvider:
        return cls()

    async def send(
        self,
        model: str,
        messages: list[Message],
        tools: list,
        prompts: list[Message] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        guide_text = (
            "欢迎使用 MutBot！当前尚未配置 LLM 服务，"
            "让我先帮你完成初始设置。"
        )
        yield StreamEvent(type="text_delta", text=guide_text)

        tool_block = ToolUseBlock(
            id="setup_" + uuid4().hex[:10],
            name="Setup-llm",
            input={},
        )
        yield StreamEvent(type="response_done", response=Response(
            message=Message(role="assistant", blocks=[
                TextBlock(text=guide_text),
                tool_block,
            ]),
            stop_reason="tool_use",
        ))


class GuideSession(AgentSession):
    """向导 Agent Session"""

    display_name = "Guide"
    display_icon = "circle-question-mark"

    system_prompt: str = (
        "你是 MutBot 助手，帮助用户了解和使用 MutBot 的各项功能。\n"
        "\n"
        "核心能力：\n"
        "- 友好地介绍 MutBot 的功能和使用方式\n"
        "- 回答用户的各类问题，提供信息和建议\n"
        "- 通过 Web 搜索获取最新信息来回答问题\n"
        "- 搜索前先思考合适的关键词和搜索策略\n"
        "- 对搜索结果进行交叉验证，注意信息的时效性\n"
        "- 给出结论时标注信息来源"
    )

    def create_agent(
        self,
        config: Config,
        log_dir: Path | None = None,
        session_ts: str = "",
        messages: list[Message] | None = None,
        **kwargs: Any,
    ) -> Agent:
        """组装向导 Agent，配备 WebToolkit 和 SessionToolkit。"""
        from mutagent.client import LLMClient
        from mutagent.toolkits.web_toolkit import WebToolkit
        from mutbot.toolkits.session_toolkit import SessionToolkit
        from mutbot.runtime.session_impl import setup_environment, create_llm_client
        import mutagent.builtins.web_local  # noqa: F401  -- 注册 LocalFetchImpl

        setup_environment(config)

        # 有 LLM 配置 → 使用真实 provider；无配置 → NullProvider 占位
        if config.get("providers"):
            client = create_llm_client(config, self.model, log_dir, session_ts)
        else:
            client = LLMClient(
                provider=NullProvider(),
                model="setup-wizard",
            )

        tool_set = ToolSet()

        web_tools = WebToolkit(config=config)
        tool_set.add(web_tools)

        session_manager = kwargs.get("session_manager")
        session_tools = SessionToolkit(
            session_manager=session_manager,
            workspace_id=self.workspace_id,
        )
        tool_set.add(session_tools)

        # Setup 模式：添加 SetupToolkit 用于交互式 LLM 配置
        # 正常模式也保留，允许用户随时重新配置 LLM
        from mutbot.builtins.setup_toolkit import SetupToolkit
        tool_set.add(SetupToolkit())

        agent = Agent(
            llm=client,
            tools=tool_set,
            context=AgentContext(
                prompts=[Message(role="system", blocks=[TextBlock(text=self.system_prompt)], label="base")],
                messages=messages if messages is not None else [],
            ),
        )
        tool_set.agent = agent
        return agent
