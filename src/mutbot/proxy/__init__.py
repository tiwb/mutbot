"""mutbot.proxy — LLM 代理模块。

import 此模块时加载 View 子类（LlmInfoView、LlmModelsView 等），
自动注册到 Router。
"""

# import routes 模块以注册 View 子类
import mutbot.proxy.routes as _routes  # noqa: F401

__all__: list[str] = []
