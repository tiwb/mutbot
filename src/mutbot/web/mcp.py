"""MCP 运行时入口 — MutBotMCP 视图。

只暴露 ``pysandbox`` 一个 tool(在 mutagent 层注册),其余调试能力迁移至
sandbox namespace ``mutbot.*``,见 ``mutbot/builtins/debug_tools.py``。
"""

from __future__ import annotations

import mutbot
from mutagent.net.mcp import MCPView


class MutBotMCP(MCPView):
    path = "/mcp"
    name = "mutbot"
    version = mutbot.__version__
    instructions = (
        "MutBot Python 沙箱 — 聚合外部 MCP、CLI 工具和内置能力为命名空间函数,"
        "通过 pysandbox(code=...) 执行 Python 代码。"
        "能力清单见 help();具体命名空间见 help(<namespace>),如 help(mutbot) / help(web)。"
    )
