"""MCP 运行时入口 — MutBotMCP 视图。

只暴露 ``pysandbox`` 一个 tool(在 mutagent 层注册),其余调试能力迁移至
sandbox namespace ``mutbot.*``,见 ``mutbot/builtins/debug_tools.py``。
"""

from __future__ import annotations

import mutbot
from mutagent.net.mcp import MCPView
from mutio.mcp import MCPPromptSet


class MutBotMCP(MCPView):
    path = "/mcp"
    name = "mutbot"
    version = mutbot.__version__
    instructions = (
        "MutBot Python 沙箱 — 聚合外部 MCP、CLI 工具和内置能力为命名空间函数,"
        "通过 pysandbox(code=...) 执行 Python 代码。"
        "能力清单见 help();具体命名空间见 help(<namespace>)。"
    )


class PysandboxReadme(MCPPromptSet):
    """pysandbox 入门说明 prompt。"""

    view = MutBotMCP

    def readme(self) -> str:
        """注入 mutbot 简介与 pysandbox 使用范式 — 进入陌生项目用 mutbot 前先触发一次。"""
        return _README


_README = """\
# mutbot — pysandbox 使用须知

你刚刚连接到 mutbot MCP server。这份 readme 介绍 mutbot 是什么、能力如何
组织、典型任务怎么入手。后续在本会话里使用 `pysandbox` 时请遵循这里的范式。

## mutbot 是什么

mutbot 是一个常驻本地的 AI agent runtime + Web UI。它不是一个传统意义的
"工具集合 MCP",而是把**自身全部运行时能力**通过单一沙箱入口暴露:外部
MCP server、CLI 工具、内置调试能力,统统聚合成 Python namespace,在
`pysandbox` 中按 `<namespace>.<func>(...)` 调用。

这种设计意味着:

- **MCP 这一侧只看到一个 tool**:`pysandbox`
- **真正的能力清单是动态的**,由 mutbot 配置决定,需要在沙箱里自省
- **新增能力不用扩 MCP 协议**,加一个 namespace 即可

## 第一步:摸清当前实例有哪些 namespace

```python
pysandbox(code="help()")
```

返回当前可用的全部 namespace 列表(每个实例配置不同,清单不固定)。

## 第二步:进入感兴趣的 namespace 看函数

```python
pysandbox(code="help(mutbot)")    # mutbot 自身的运行时内省/日志/热重启
pysandbox(code="help(web)")        # web 抓取(若配置了 web namespace)
pysandbox(code="help(<其他>)")     # 其他配置的 namespace
```

`help(<namespace>)` 列出该 namespace 下所有函数及一行说明。
`help(<namespace>.<func>)` 看完整签名和 docstring。

## 任务向 prompt(若该实例配置了)

针对特定任务(如 playwright、特定调试工作流),mutbot 实例可能注册了
专门的 prompt。在 host 的 `/` 菜单里查看 `mcp__mutbot__*` 看是否有对应
任务的预设——比 readme 这种通识更精准。

## 一句话原则

**先 `help()` 再调用,不要凭印象写 namespace 函数名**——清单是动态的,
不同 mutbot 实例配置不同,自省才是可靠路径。
"""
