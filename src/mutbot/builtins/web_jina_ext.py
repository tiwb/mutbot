"""mutbot.builtins.web_jina_ext -- Jina 实现覆盖，附加 Config-update 配置指引。

覆盖 mutagent 的 JinaSearchImpl.search 和 JinaFetchImpl.fetch，
在 Jina 配额错误时附加 mutbot 特有的 Config-update 工具使用说明。
"""

from __future__ import annotations

import mutagent
from mutagent.builtins.web_jina import (
    JinaFetchImpl,
    JinaSearchImpl,
    _jina_fetch as _base_fetch,
    _jina_search as _base_search,
)

_CONFIG_HINT = (
    "\n\n可能是免费额度已用完。请直接调用 Config-update 工具配置 API Key"
    "（不要使用 UI-show，Config-update 自带输入界面）：\n"
    '  key: "WebToolkit.jina_api_key"\n'
    '  description: "Jina API Key。获取地址：https://jina.ai/api-key"'
)


def _enrich_error(exc: RuntimeError) -> RuntimeError:
    """为 Jina 配额错误附加 Config-update 指引。"""
    msg = str(exc)
    if "401" in msg or "429" in msg:
        return RuntimeError(msg + _CONFIG_HINT)
    return exc


@mutagent.impl(JinaSearchImpl.search)
async def _jina_search(self: JinaSearchImpl, query: str, max_results: int = 5) -> str:
    try:
        return await _base_search(self, query, max_results)
    except RuntimeError as e:
        raise _enrich_error(e) from e


@mutagent.impl(JinaFetchImpl.fetch)
async def _jina_fetch(self: JinaFetchImpl, url: str, format: str = "markdown") -> str:
    try:
        return await _base_fetch(self, url, format)
    except RuntimeError as e:
        raise _enrich_error(e) from e
