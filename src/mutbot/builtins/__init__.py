"""mutbot 内置 Declaration 子类。

导入所有内置模块，确保 mutobj 子类发现机制能找到它们。
同时触发 @impl 注册（TerminalSession 通信回调、debug_tools namespace）。
"""

import mutbot.builtins.menus as menus  # noqa: F401
import mutbot.builtins.debug_tools as debug_tools  # noqa: F401
import mutbot.runtime.terminal  # noqa: F401  ← 触发 TerminalSession @impl
