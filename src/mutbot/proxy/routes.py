"""mutbot.proxy.routes -- LLM 代理路由 View 子类。

挂载到 /llm 前缀，将外部请求代理到配置的 LLM 后端。
/llm 根路径提供 API 说明页面。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import mutobj

from mutagent.net.client import HttpClient
from mutagent.provider import LLMProvider

from mutbot.proxy.translation import (
    anthropic_request_to_openai,
    normalize_model_name,
    openai_response_to_anthropic,
    openai_sse_to_anthropic_events,
)
from mutagent.net.server import View, Request, Response, StreamingResponse, json_response, html_response

logger = logging.getLogger(__name__)

# 模块级配置（在 lifespan 中初始化）
_providers_config: dict[str, dict[str, Any]] = {}
_provider_instances: dict[str, LLMProvider] = {}


def initialize_providers(providers_config: dict[str, dict[str, Any]]) -> None:
    """从 providers config 创建并缓存 provider 实例。

    启动时和 config 变更时调用。
    """
    global _providers_config, _provider_instances
    _providers_config = providers_config

    # 确保内置 provider 已注册
    import mutagent.builtins.anthropic_provider  # noqa: F401
    import mutagent.builtins.openai_provider  # noqa: F401

    instances: dict[str, LLMProvider] = {}
    for name, conf in providers_config.items():
        provider_path = conf.get("provider", "AnthropicProvider")
        try:
            provider_cls = mutobj.resolve_class(provider_path, base_cls=LLMProvider)
            instances[name] = provider_cls.from_spec(conf)
            logger.info("Proxy provider initialized: %s (%s)", name, provider_path)
        except Exception:
            logger.warning("Failed to initialize proxy provider '%s'", name, exc_info=True)
    _provider_instances = instances


# ---------------------------------------------------------------------------
# View 子类
# ---------------------------------------------------------------------------

class LlmInfoView(View):
    """LLM API 说明页面。"""
    path = "/llm"

    async def get(self, request: Request) -> Response:
        return html_response(_render_info_page())


class LlmInfoSlashView(View):
    """LLM API 说明页面（带尾斜杠）。"""
    path = "/llm/"

    async def get(self, request: Request) -> Response:
        return html_response(_render_info_page())


class LlmModelsView(View):
    """列出所有已配置的模型。"""
    path = "/llm/v1/models"

    async def get(self, request: Request) -> Response:
        models = _get_all_models()
        data = []
        for m in models:
            data.append({
                "id": m["name"],
                "object": "model",
                "created": 0,
                "owned_by": m["provider_name"],
                "model_id": m["model_id"],
            })
        return json_response({"object": "list", "data": data})


class LlmMessagesView(View):
    """Anthropic Messages 格式代理端点。"""
    path = "/llm/v1/messages"

    async def post(self, request: Request) -> Response | StreamingResponse:
        body = await request.json()
        return await _proxy_request(body, client_format="anthropic")


class LlmCompletionsView(View):
    """OpenAI Chat Completions 格式代理端点。"""
    path = "/llm/v1/chat/completions"

    async def post(self, request: Request) -> Response | StreamingResponse:
        body = await request.json()
        return await _proxy_request(body, client_format="openai")


# ---------------------------------------------------------------------------
# Model resolution (provider 顺序搜索)
# ---------------------------------------------------------------------------

def _find_model_config(model_name: str) -> tuple[dict[str, Any], LLMProvider] | None:
    """根据模型名查找配置和 provider 实例。按 provider 顺序搜索。"""
    normalized = normalize_model_name(model_name)

    for prov_name, prov_conf in _providers_config.items():
        models = prov_conf.get("models", [])
        if isinstance(models, list):
            for mid in models:
                if mid == model_name or normalize_model_name(mid) == normalized:
                    result = {k: v for k, v in prov_conf.items() if k != "models"}
                    result["model_id"] = mid
                    instance = _provider_instances.get(prov_name)
                    if instance is not None:
                        return result, instance
                    return None
        elif isinstance(models, dict):
            if model_name in models:
                result = {k: v for k, v in prov_conf.items() if k != "models"}
                result["model_id"] = models[model_name]
                instance = _provider_instances.get(prov_name)
                if instance is not None:
                    return result, instance
                return None

    return None


def _get_all_models() -> list[dict[str, str]]:
    """展开所有 provider 的模型列表。"""
    result: list[dict[str, str]] = []
    for prov_name, prov_conf in _providers_config.items():
        provider_cls = prov_conf.get("provider", "mutagent.builtins.anthropic_provider.AnthropicProvider")
        models = prov_conf.get("models", [])
        if isinstance(models, list):
            for model_id in models:
                result.append({
                    "name": model_id,
                    "model_id": model_id,
                    "provider": provider_cls,
                    "provider_name": prov_name,
                })
        elif isinstance(models, dict):
            for alias, model_id in models.items():
                result.append({
                    "name": alias,
                    "model_id": model_id,
                    "provider": provider_cls,
                    "provider_name": prov_name,
                })
    return result


# ---------------------------------------------------------------------------
# Backend info
# ---------------------------------------------------------------------------

def _get_backend_info(
    provider: LLMProvider,
) -> tuple[str, str, dict[str, str]]:
    """从 provider 实例获取后端信息：(base_url, target_format, headers)。"""
    from mutbot.copilot.provider import CopilotProvider
    from mutagent.builtins.openai_provider import OpenAIProvider

    if isinstance(provider, CopilotProvider):
        base_url = provider.auth.get_base_url(provider.account_type)
        headers = provider.auth.get_headers()
        return base_url, "openai", headers

    elif isinstance(provider, OpenAIProvider):
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        return provider.base_url, "openai", headers

    else:
        # AnthropicProvider 及其他
        base_url = getattr(provider, 'base_url', 'https://api.anthropic.com')
        api_key = getattr(provider, 'api_key', '')
        headers = {
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        return base_url, "anthropic", headers


# ---------------------------------------------------------------------------
# Proxy logic
# ---------------------------------------------------------------------------

async def _proxy_request(
    body: dict[str, Any],
    client_format: str,
) -> Response | StreamingResponse:
    """统一代理处理逻辑。"""
    model_name = body.get("model", "")
    stream = body.get("stream", False)

    found = _find_model_config(model_name)
    if found is None:
        return json_response(
            {"error": {"message": f"Model not found: {model_name}"}},
            status=404,
        )

    model_config, provider = found
    base_url, target_format, headers = _get_backend_info(provider)
    actual_model = model_config.get("model_id", model_name)

    # 构建后端请求
    if client_format == target_format:
        backend_body = {**body, "model": normalize_model_name(actual_model)}
        if target_format == "anthropic":
            endpoint = f"{base_url}/v1/messages"
        else:
            endpoint = f"{base_url}/chat/completions"
    elif client_format == "anthropic" and target_format == "openai":
        backend_body = anthropic_request_to_openai(body)
        backend_body["model"] = normalize_model_name(actual_model)
        endpoint = f"{base_url}/chat/completions"
    elif client_format == "openai" and target_format == "anthropic":
        return json_response(
            {"error": {"message": "OpenAI-to-Anthropic proxy not implemented"}},
            status=501,
        )
    else:
        return json_response(
            {"error": {"message": f"Unknown format: {client_format} → {target_format}"}},
            status=400,
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
) -> Response:
    """非流式代理。"""
    body.pop("stream", None)

    async with HttpClient.create(timeout=120.0) as client:
        resp = await client.post(endpoint, headers=headers, json=body)

    duration_ms = int((time.monotonic() - t0) * 1000)
    data = resp.json()

    if resp.status_code != 200:
        logger.warning("Proxy backend error (%d): %s", resp.status_code, resp.text[:200])
        return json_response(data, status=resp.status_code)

    # 响应格式转换
    if client_format != target_format:
        if target_format == "openai" and client_format == "anthropic":
            data = openai_response_to_anthropic(data, model=model)

    _log_proxy_call(client_format, model, body, data, duration_ms)
    return json_response(data)


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
        async with HttpClient.create(timeout=120.0) as client:
            async with client.stream(
                "POST", endpoint, headers=headers, json=body
            ) as resp:
                if resp.status_code != 200:
                    error_text = ""
                    async for chunk in resp.aiter_text():
                        error_text += chunk
                    logger.warning("Proxy stream error (%d): %s",
                                   resp.status_code, error_text[:200])
                    yield f"data: {json.dumps({'error': {'message': error_text[:500]}})}\n\n".encode()
                    return

                if client_format == target_format:
                    # 同格式 → 直接透传
                    async for line in resp.aiter_lines():
                        yield f"{line}\n".encode()
                        if line == "":
                            yield b"\n"
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
                        yield f"event: {event_type}\ndata: {event_data}\n\n".encode()

        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info("Proxy stream completed (model=%s, duration=%dms)", model, duration_ms)

    return StreamingResponse(
        body_iterator=event_generator(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
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


# ---------------------------------------------------------------------------
# /llm info page
# ---------------------------------------------------------------------------

def _render_info_page() -> str:
    """渲染 /llm 信息页 HTML。"""
    # 构建模型列表 HTML
    all_models = _get_all_models()
    if all_models:
        rows = ""
        for m in all_models:
            rows += (
                f"<tr><td><code>{m['name']}</code></td>"
                f"<td><code>{m['model_id']}</code></td>"
                f"<td>{m['provider_name']}</td>"
                f"<td><code>{m['provider']}</code></td></tr>\n"
            )
        models_table = f"""
        <table>
            <thead><tr>
                <th>Name</th><th>Model ID</th><th>Provider</th><th>Provider Class</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>"""
    else:
        models_table = "<p><em>No models configured.</em></p>"

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MutBot LLM API</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 900px; margin: 40px auto; padding: 0 20px; color: #333;
         line-height: 1.6; }}
  h1 {{ border-bottom: 2px solid #eee; padding-bottom: 10px; }}
  h2 {{ margin-top: 2em; color: #555; }}
  code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  pre {{ background: #f8f8f8; padding: 16px; border-radius: 6px; overflow-x: auto;
         border: 1px solid #e0e0e0; }}
  pre code {{ background: none; padding: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #f4f4f4; }}
  .endpoint {{ margin: 1em 0; padding: 12px 16px; background: #f8f9fa;
               border-left: 4px solid #4a9eff; border-radius: 4px; }}
  .method {{ font-weight: bold; color: #4a9eff; }}
  .method.post {{ color: #49cc90; }}
</style>
</head>
<body>

<h1>MutBot LLM API</h1>
<p>OpenAI and Anthropic compatible LLM proxy endpoints.</p>

<h2>API Endpoints</h2>

<div class="endpoint">
  <span class="method">GET</span> <code>/llm/v1/models</code>
  <p>List all configured models.</p>
</div>

<div class="endpoint">
  <span class="method post">POST</span> <code>/llm/v1/chat/completions</code>
  <p>OpenAI Chat Completions format. Send requests in OpenAI format, responses in OpenAI format.</p>
</div>

<div class="endpoint">
  <span class="method post">POST</span> <code>/llm/v1/messages</code>
  <p>Anthropic Messages format. Send requests in Anthropic format, responses in Anthropic format.</p>
</div>

<p>Format translation is automatic: if the backend uses a different format than the client,
the proxy handles conversion transparently.</p>

<h2>Configured Models</h2>
{models_table}

<h2>Configuration</h2>

<p>Config file: <code>~/.mutbot/config.json</code> (user-level) or <code>.mutbot/config.json</code> (project-level).</p>

<h3>Provider-based format</h3>
<p>Each provider is configured once with its connection details. Models are listed under the provider.</p>

<p><strong>List form</strong> (model name = model_id):</p>
<pre><code>{{
  "default_model": "claude-sonnet-4",
  "providers": {{
    "anthropic": {{
      "provider": "AnthropicProvider",
      "base_url": "https://api.anthropic.com",
      "auth_token": "$ANTHROPIC_API_KEY",
      "models": ["claude-sonnet-4", "claude-haiku-4.5"]
    }}
  }}
}}</code></pre>

<p><strong>Dict form</strong> (alias &rarr; model_id, for resolving name conflicts):</p>
<pre><code>{{
  "default_model": "copilot-claude",
  "providers": {{
    "copilot": {{
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "github_token": "ghu_xxx",
      "models": {{
        "copilot-claude": "claude-sonnet-4",
        "copilot-gpt": "gpt-4.1"
      }}
    }}
  }}
}}</code></pre>

<p>Environment variables are supported: use <code>$VAR</code> or <code>${{VAR}}</code> syntax for sensitive values.</p>

</body>
</html>"""
