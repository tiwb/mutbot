"""mutbot 内置 Declaration 子类。

导入所有内置模块，确保 mutobj 子类发现机制能找到它们。
同时触发 @impl 注册（TerminalSession / AgentSession 通信回调）。
"""

import mutbot.builtins.menus as menus  # noqa: F401
import mutbot.builtins.config_toolkit as config_toolkit  # noqa: F401
import mutbot.builtins.web_jina_ext as web_jina_ext  # noqa: F401
import mutbot.builtins.http_client as http_client  # noqa: F401
import mutbot.runtime.terminal  # noqa: F401  ← 触发 TerminalSession @impl
import mutbot.runtime.agent_bridge  # noqa: F401  ← 触发 AgentSession @impl
