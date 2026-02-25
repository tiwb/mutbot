"""研究员 Agent Session — 信息检索与分析专家。

通过 Web 搜索和网页内容读取来回答用户问题，
整理分析检索到的信息，给出结论和下一步建议。
不直接执行任何操作（不修改文件、不运行命令）。
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


class ResearcherSession(AgentSession):
    """研究员 Agent Session"""

    system_prompt: str = (
        "你是一个研究分析助手，擅长信息检索和分析。\n"
        "\n"
        "核心能力：\n"
        "- 通过搜索和阅读网页内容来回答问题\n"
        "- 整理、对比和分析检索到的信息\n"
        "- 给出有依据的分析结论和可行的下一步建议\n"
        "\n"
        "工作原则：\n"
        "- 你只负责研究和分析，不直接执行操作\n"
        "- 搜索前先思考合适的关键词和搜索策略\n"
        "- 对搜索结果进行交叉验证，注意信息的时效性\n"
        "- 如果信息不足，主动进行补充搜索\n"
        "- 给出结论时要标注信息来源"
    )

    def create_agent(
        self,
        config: Config,
        log_dir: Path | None = None,
        session_ts: str = "",
        messages: list[Message] | None = None,
        **kwargs: Any,
    ) -> Agent:
        """组装研究员 Agent，配备 WebToolkit。"""
        from mutagent.client import LLMClient
        from mutagent.toolkits.web_toolkit import WebToolkit
        from mutbot.runtime.session_impl import setup_environment, create_llm_client

        setup_environment(config)
        client = create_llm_client(config, self.model, log_dir, session_ts)

        web_tools = WebToolkit(config=config)
        tool_set = ToolSet()
        tool_set.add(web_tools)

        agent = Agent(
            client=client,
            tool_set=tool_set,
            system_prompt=self.system_prompt,
            messages=messages if messages is not None else [],
        )
        tool_set.agent = agent
        return agent
