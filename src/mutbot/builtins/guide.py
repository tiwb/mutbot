"""向导 Agent Session — 用户首先接触的入口。

引导使用、识别需求类型、创建专业 Agent Session 并传达用户需求。
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
        "你是 MutBot 的向导，帮助用户了解和使用 MutBot 的各项功能。\n"
        "\n"
        "核心职责：\n"
        "- 友好地介绍 MutBot 的功能和使用方式\n"
        "- 识别用户的具体需求类型（研究、编码、文件操作等）\n"
        "- 当识别到具体需求时，为用户创建合适的专业 Agent Session\n"
        "- 创建 Session 时要清晰地向专业 Agent 传达用户的需求\n"
        "\n"
        "可用的专业 Agent 类型：\n"
        "- mutbot.builtins.researcher.ResearcherSession：研究员，擅长 Web 搜索和信息分析\n"
        "\n"
        "工作原则：\n"
        "- 能处理简单对话和引导类问题（自我介绍、功能说明、简单问答）\n"
        "- 仅当自身无法帮助用户时，才委托给专业 Agent\n"
        "- 委托时要准确理解并传达用户需求"
    )

    def create_agent(
        self,
        config: Config,
        log_dir: Path | None = None,
        session_ts: str = "",
        messages: list[Message] | None = None,
        **kwargs: Any,
    ) -> Agent:
        """组装向导 Agent，配备 SessionToolkit。"""
        from mutagent.client import LLMClient
        from mutbot.toolkits.session_toolkit import SessionToolkit
        from mutbot.runtime.session_impl import setup_environment, create_llm_client

        setup_environment(config)
        client = create_llm_client(config, self.model, log_dir, session_ts)

        session_manager = kwargs.get("session_manager")
        session_tools = SessionToolkit(
            session_manager=session_manager,
            workspace_id=self.workspace_id,
        )

        tool_set = ToolSet()
        tool_set.add(session_tools)

        agent = Agent(
            client=client,
            tool_set=tool_set,
            system_prompt=self.system_prompt,
            messages=messages if messages is not None else [],
        )
        tool_set.agent = agent
        return agent
