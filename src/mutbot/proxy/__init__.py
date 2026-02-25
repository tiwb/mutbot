"""mutbot.proxy — LLM 代理模块。

通过 modules 配置加载：
    "modules": ["mutbot.proxy"]

import 此模块时不会产生副作用，仅暴露 create_llm_router。
"""

from mutbot.proxy.routes import create_llm_router

__all__ = ["create_llm_router"]
