"""mutbot HttpClient @impl — User-Agent 覆盖为 MutBot.ai/{version}。"""

from __future__ import annotations

from typing import Any

import httpx

import mutbot
from mutagent import impl
from mutagent.net.client import HttpClient


@impl(HttpClient.create)
def _create(*, user_agent: str | None = None, **kwargs: Any) -> httpx.AsyncClient:
    ua = user_agent or f"MutBot.ai/{mutbot.__version__}"
    headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("user-agent", ua)
    kwargs["headers"] = headers
    return httpx.AsyncClient(**kwargs)
