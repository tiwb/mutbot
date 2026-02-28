"""向导 Agent Session — 用户首先接触的入口。

MutBot 的默认会话类型，具备 Web 搜索能力，可直接回答用户问题，
也可以创建专业 Agent Session 来处理特定任务。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from mutagent.agent import Agent
from mutagent.tools import ToolSet

from mutbot.session import AgentSession

if TYPE_CHECKING:
    from mutagent.config import Config
    from mutagent.messages import Message


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

        # Setup 模式：无 LLM 配置 或 force_setup 时使用 SetupProvider
        force_setup = self.config.get("force_setup", False)
        if config.get("providers") and not force_setup:
            client = create_llm_client(config, self.model, log_dir, session_ts)
        else:
            from mutbot.builtins.setup_provider import SetupProvider
            client = LLMClient(
                provider=SetupProvider(),
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

        agent = Agent(
            client=client,
            tool_set=tool_set,
            system_prompt=self.system_prompt,
            messages=messages if messages is not None else [],
        )
        tool_set.agent = agent
        return agent
