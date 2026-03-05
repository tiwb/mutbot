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
    "[ACTION REQUIRED] You MUST call Config-update now to set the API key. "
    "Do NOT reply to the user first.\n\n"
    'Config-update view: {"title": "Configure Jina API Key", "components": ['
    '{"type": "text", "id": "WebToolkit.jina_api_key", "label": "Jina API Key", "secret": true, "placeholder": "jina_xxx..."}, '
    '{"type": "hint", "id": "__help", "text": "Get your key at https://jina.ai/api-key"}'
    "]}\n\n"
    "Original error: "
)


def _enrich_error(exc: RuntimeError) -> RuntimeError:
    """Prepend Config-update instruction to Jina auth/quota errors."""
    msg = str(exc)
    if "401" in msg or "429" in msg:
        return RuntimeError(_CONFIG_HINT + msg)
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
