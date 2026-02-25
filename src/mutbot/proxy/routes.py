"""mutbot.proxy.routes -- LLM 代理 FastAPI 路由。

挂载到 /llm 前缀，将外部请求代理到配置的 LLM 后端。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mutbot.proxy.translation import (
    anthropic_request_to_openai,
    normalize_model_name,
    openai_response_to_anthropic,
    openai_sse_to_anthropic_events,
)

logger = logging.getLogger(__name__)

# 模块级配置（在 create_llm_router 中初始化）
_models_config: dict[str, dict[str, Any]] = {}
_copilot_auth: Any = None  # 延迟 import CopilotAuth


def create_llm_router(config: dict[str, Any]) -> APIRouter:
    """创建 LLM 代理路由。

    Args:
        config: mutagent 配置 dict（含 models 和 default_model）。
    """
    global _models_config
    _models_config = config.get("models", {})

    router = APIRouter()

    @router.get("/v1/models")
    async def list_models() -> JSONResponse:
        """列出所有已配置的模型。"""
        models = []
        for name, model_config in _models_config.items():
            models.append({
                "id": model_config.get("model_id", name),
                "object": "model",
                "created": 0,
                "owned_by": model_config.get("provider", "unknown"),
            })
        return JSONResponse({"object": "list", "data": models})

    @router.post("/v1/messages")
    async def proxy_anthropic(request: Request) -> StreamingResponse | JSONResponse:
        """Anthropic Messages 格式代理端点。"""
        body = await request.json()
        return await _proxy_request(body, client_format="anthropic")

    @router.post("/v1/chat/completions")
    async def proxy_openai(request: Request) -> StreamingResponse | JSONResponse:
        """OpenAI Chat Completions 格式代理端点。"""
        body = await request.json()
        return await _proxy_request(body, client_format="openai")

    return router


def _find_model_config(model_name: str) -> dict[str, Any] | None:
    """根据模型名查找配置。支持 model_id 匹配和归一化匹配。"""
    normalized = normalize_model_name(model_name)

    # 1. 直接按 config key 匹配
    if model_name in _models_config:
        return _models_config[model_name]

    # 2. 按 model_id 匹配
    for _name, conf in _models_config.items():
        mid = conf.get("model_id", "")
        if mid == model_name or normalize_model_name(mid) == normalized:
            return conf

    return None


def _get_backend_info(
    model_config: dict[str, Any],
) -> tuple[str, str, dict[str, str]]:
    """获取后端信息：(base_url, target_format, headers)。

    Returns:
        (base_url, target_format, headers)
        target_format: "anthropic" 或 "openai"
    """
    provider = model_config.get("provider", "AnthropicProvider")

    if "CopilotProvider" in provider:
        # Copilot 后端 → OpenAI 格式
        from mutbot.copilot.auth import CopilotAuth
        auth = CopilotAuth.get_instance()
        base_url = auth.get_base_url(model_config.get("account_type", "individual"))
        headers = auth.get_headers()
        return base_url, "openai", headers

    elif "OpenAIProvider" in provider:
        base_url = model_config.get("base_url", "https://api.openai.com/v1")
        headers = {
            "Authorization": f"Bearer {model_config.get('auth_token', '')}",
            "Content-Type": "application/json",
        }
        return base_url, "openai", headers

    else:
        # Anthropic 后端
        base_url = model_config.get("base_url", "https://api.anthropic.com")
        headers = {
            "Authorization": f"Bearer {model_config.get('auth_token', '')}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        return base_url, "anthropic", headers


async def _proxy_request(
    body: dict[str, Any],
    client_format: str,
) -> StreamingResponse | JSONResponse:
    """统一代理处理逻辑。

    Args:
        body: 客户端请求 body。
        client_format: 客户端使用的格式（"anthropic" 或 "openai"）。
    """
    model_name = body.get("model", "")
    stream = body.get("stream", False)

    model_config = _find_model_config(model_name)
    if model_config is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": f"Model not found: {model_name}"}},
        )

    base_url, target_format, headers = _get_backend_info(model_config)
    actual_model = model_config.get("model_id", model_name)

    # 构建后端请求
    if client_format == target_format:
        # 同格式 → 透传（更新 model 名）
        backend_body = {**body, "model": normalize_model_name(actual_model)}
        if target_format == "anthropic":
            endpoint = f"{base_url}/v1/messages"
        else:
            endpoint = f"{base_url}/chat/completions"
    elif client_format == "anthropic" and target_format == "openai":
        # Anthropic → OpenAI 转换
        backend_body = anthropic_request_to_openai(body)
        backend_body["model"] = normalize_model_name(actual_model)
        endpoint = f"{base_url}/chat/completions"
    elif client_format == "openai" and target_format == "anthropic":
        # OpenAI → Anthropic（暂不支持此方向，先返回错误）
        return JSONResponse(
            status_code=501,
            content={"error": {"message": "OpenAI-to-Anthropic proxy not implemented"}},
        )
    else:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"Unknown format: {client_format} → {target_format}"}},
        )

    t0 = time.monotonic()

    if stream:
        return await _proxy_stream(
            endpoint, headers, backend_body,
            client_format, target_format, model_name, t0,
        )
    else:
        return await _proxy_no_stream(
            endpoint, headers, backend_body,
            client_format, target_format, model_name, t0,
        )


async def _proxy_no_stream(
    endpoint: str,
    headers: dict[str, str],
    body: dict[str, Any],
    client_format: str,
    target_format: str,
    model: str,
    t0: float,
) -> JSONResponse:
    """非流式代理。"""
    body.pop("stream", None)

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(endpoint, headers=headers, json=body)

    duration_ms = int((time.monotonic() - t0) * 1000)
    data = resp.json()

    if resp.status_code != 200:
        logger.warning("Proxy backend error (%d): %s", resp.status_code, resp.text[:200])
        return JSONResponse(status_code=resp.status_code, content=data)

    # 响应格式转换
    if client_format != target_format:
        if target_format == "openai" and client_format == "anthropic":
            data = openai_response_to_anthropic(data, model=model)

    _log_proxy_call(client_format, model, body, data, duration_ms)
    return JSONResponse(content=data)


async def _proxy_stream(
    endpoint: str,
    headers: dict[str, str],
    body: dict[str, Any],
    client_format: str,
    target_format: str,
    model: str,
    t0: float,
) -> StreamingResponse:
    """流式代理。"""
    body["stream"] = True
    if target_format == "openai":
        body["stream_options"] = {"include_usage": True}

    async def event_generator():
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", endpoint, headers=headers, json=body
            ) as resp:
                if resp.status_code != 200:
                    error_text = ""
                    async for chunk in resp.aiter_text():
                        error_text += chunk
                    logger.warning("Proxy stream error (%d): %s",
                                   resp.status_code, error_text[:200])
                    yield f"data: {json.dumps({'error': {'message': error_text[:500]}})}\n\n"
                    return

                if client_format == target_format:
                    # 同格式 → 直接透传
                    async for line in resp.aiter_lines():
                        yield f"{line}\n"
                        if line == "":
                            yield "\n"
                elif target_format == "openai" and client_format == "anthropic":
                    # OpenAI SSE → Anthropic SSE 转换
                    lines: list[str] = []
                    async for line in resp.aiter_lines():
                        lines.append(line)

                    def line_iter():
                        yield from lines

                    for event_type, event_data in openai_sse_to_anthropic_events(
                        line_iter(), model=model
                    ):
                        yield f"event: {event_type}\ndata: {event_data}\n\n"

        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info("Proxy stream completed (model=%s, duration=%dms)", model, duration_ms)

    media_type = (
        "text/event-stream"
        if client_format == "anthropic"
        else "text/event-stream"
    )
    return StreamingResponse(
        event_generator(),
        media_type=media_type,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _log_proxy_call(
    client_format: str,
    model: str,
    request_body: dict[str, Any],
    response_data: dict[str, Any],
    duration_ms: int,
) -> None:
    """记录代理调用日志（简要）。"""
    logger.info(
        "Proxy call: format=%s model=%s duration=%dms",
        client_format, model, duration_ms,
    )
