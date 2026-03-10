"""mutbot HttpClient @impl — User-Agent 覆盖为 MutBot.ai/{version}。"""

from __future__ import annotations

from typing import Any

import httpx

import mutbot
from mutagent import impl
from mutagent.http import HttpClient


@impl(HttpClient.create)
def _create(**kwargs: Any) -> httpx.AsyncClient:
    headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("user-agent", f"MutBot.ai/{mutbot.__version__}")
    kwargs["headers"] = headers
    return httpx.AsyncClient(**kwargs)
