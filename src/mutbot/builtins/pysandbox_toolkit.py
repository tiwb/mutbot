"""mutbot.builtins.pysandbox_toolkit -- PySandboxToolkit。

Agent 工具：在 sandbox 中执行 Python 代码，per-agent state 隔离。
"""

from __future__ import annotations

import asyncio
from typing import Any

from mutagent.tools import Toolkit
from mutagent.sandbox.app import SandboxApp


class PySandboxToolkit(Toolkit):
    """Python 沙箱执行环境。

    Attributes:
        _tool_prefix: 空字符串，使工具名直接为方法名。
    """

    _tool_prefix = ""
    _tool_methods = ["pysandbox"]

    _app: SandboxApp
    _state: dict[str, Any]

    async def pysandbox(self, code: str) -> str:
        """Execute Python code in a sandboxed environment.

All available functions are pre-injected as namespace objects.
Use help(func) for detailed documentation.
import is not supported.
"""
        ...


# ---------------------------------------------------------------------------
# 实现
# ---------------------------------------------------------------------------

import mutagent  # noqa: E402
from mutagent.sandbox.tools import format_exec_result  # noqa: E402


@mutagent.impl(PySandboxToolkit.pysandbox)
async def _pysandbox(self: PySandboxToolkit, code: str) -> str:
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, self._app.exec_code, code, self._state)

    text, _is_error = format_exec_result(result)
    return text
