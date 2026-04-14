"""mutbot.builtins.web_tools -- WebTools NamespaceTools。

将 Web 搜索/获取能力注入 sandbox 命名空间，
复用 mutagent 的 SearchImpl/FetchImpl 发现机制。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import mutobj

from mutagent.sandbox.namespace import NamespaceTools
from mutagent.toolkits.web_toolkit import SearchImpl, FetchImpl

if TYPE_CHECKING:
    from mutagent.config import Config

logger = logging.getLogger(__name__)

# 延迟获取 config
_config: Config | None = None


def set_config(config: Config) -> None:
    global _config
    _config = config


class WebTools(NamespaceTools):
    """Web 搜索和内容获取。

    sandbox 中调用：web.search(query="...") / web.fetch(url="...")
    """

    async def search(self, query: str, max_results: int = 5) -> str:
        """搜索网页，返回结果摘要。"""
        impls = {c.name: c for c in mutobj.discover_subclasses(SearchImpl)}
        if not impls:
            return "No search implementation available."
        impl_cls = impls.get("jina") or next(iter(impls.values()))
        kwargs = {"config": _config} if _config else {}
        instance = impl_cls(**kwargs)
        return await instance.search(query, max_results)

    async def fetch(self, url: str, format: str = "markdown") -> str:
        """获取网页内容，返回文本。"""
        if format == "raw":
            from mutagent.builtins.web_toolkit_impl import _httpx_get_raw
            return await _httpx_get_raw(url)
        impls = {c.name: c for c in mutobj.discover_subclasses(FetchImpl)}
        if not impls:
            return 'No content extraction available. Use format="raw" for raw HTML.'
        impl_cls = impls.get("local") or impls.get("jina") or next(iter(impls.values()))
        kwargs = {"config": _config} if _config else {}
        instance = impl_cls(**kwargs)
        return await instance.fetch(url, format)
