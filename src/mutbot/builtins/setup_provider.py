"""mutbot.builtins.setup_provider -- 脚本化 LLM Provider（配置向导）。

无 LLM 配置时，替代真实 LLMProvider 驱动 GuideSession。
通过状态机引导用户选择 provider、输入凭证、选择模型。
配置完成后创建真实 LLMProvider，后续 send() 直接代理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from typing import AsyncIterator

from mutagent.messages import Message, Response, StreamEvent, TextBlock
from mutagent.provider import LLMProvider
from mutbot.runtime.config import MUTBOT_USER_DIR

logger = logging.getLogger(__name__)

MUTBOT_CONFIG_PATH = MUTBOT_USER_DIR / "config.json"

# VS Code Copilot Chat 使用的 Client ID（与 auth.py 一致）
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"

# --- Model discovery constants (ported from CLI setup.py) ---
_MAX_NUMBERED_MODELS = 10
_CHAT_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
_FEATURED_FAMILIES_PER_PREFIX = 2
_VARIANT_SUFFIXES = ("-mini", "-nano", "-turbo", "-latest", "-preview", "-realtime")


# ---------------------------------------------------------------------------
# Model prioritization (ported from CLI setup.py)
# ---------------------------------------------------------------------------

def _model_family(name: str) -> str:
    """提取模型 family（去掉变体后缀）。"""
    for suffix in _VARIANT_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _major_prefix(family: str) -> str:
    """提取 family 的主前缀用于分组。"""
    m = re.match(r'^([a-zA-Z]+)', family)
    return m.group(1) if m else family


def _prioritize_models(models_with_ts: list[tuple[str, int]]) -> list[str]:
    """按 family 分组，每个前缀保留最新 N 个 family，其余排后面。"""
    families: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for model_id, created in models_with_ts:
        fam = _model_family(model_id)
        families[fam].append((model_id, created))

    family_recency: dict[str, int] = {
        fam: max(c for _, c in members)
        for fam, members in families.items()
    }

    prefix_families: dict[str, list[str]] = defaultdict(list)
    for fam in families:
        prefix = _major_prefix(fam)
        prefix_families[prefix].append(fam)

    featured_set: set[str] = set()
    for _prefix, fams in prefix_families.items():
        fams_sorted = sorted(fams, key=lambda f: family_recency[f], reverse=True)
        for f in fams_sorted[:_FEATURED_FAMILIES_PER_PREFIX]:
            featured_set.add(f)

    all_families_sorted = sorted(
        families.keys(),
        key=lambda f: family_recency[f],
        reverse=True,
    )

    featured: list[str] = []
    rest: list[str] = []
    for fam in all_families_sorted:
        ids = [m for m, _ in sorted(families[fam], key=lambda x: x[0])]
        if fam in featured_set:
            featured.extend(ids)
        else:
            rest.extend(ids)

    return featured + rest


# ---------------------------------------------------------------------------
# SetupProvider
# ---------------------------------------------------------------------------

class SetupProvider(LLMProvider):
    """脚本化 LLM Provider — 配置完成后代理到真实 provider。

    实例变量维护状态机。配置完成后创建真实 LLMProvider，
    后续 send() 直接代理，同一 session 无缝切换。
    """

    def __init__(self) -> None:
        self._state: str = "WELCOME"
        self._context: dict = {}
        self._real_provider: LLMProvider | None = None
        self._real_model: str = ""

    @classmethod
    def from_config(cls, model_config: dict) -> SetupProvider:
        return cls()

    async def send(
        self,
        model: str,
        messages: list[Message],
        tools: list,
        prompts: list[Message] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        # 已完成配置 → 代理到真实 provider
        if self._real_provider:
            async for event in self._real_provider.send(
                self._real_model, messages, tools, prompts, stream
            ):
                yield event
            return

        # Setup 阶段 → 状态机
        last_user_text = ""
        for msg in reversed(messages):
            if msg.role == "user":
                for block in msg.blocks:
                    if isinstance(block, TextBlock) and block.text:
                        last_user_text = block.text.strip()
                        break
                if last_user_text:
                    break

        async for event in self._dispatch(last_user_text):
            yield event

    # ------------------------------------------------------------------
    # 事件生成辅助
    # ------------------------------------------------------------------

    async def _reply(self, text: str) -> AsyncIterator[StreamEvent]:
        """生成一条完整的文本响应（text_delta + response_done）。"""
        yield StreamEvent(type="text_delta", text=text)
        yield StreamEvent(type="response_done", response=Response(
            message=Message(role="assistant", blocks=[TextBlock(text=text)]),
            stop_reason="end_turn",
        ))

    def _choice_text(self) -> str:
        return (
            "Which provider would you like to use?\n\n"
            "1. **GitHub Copilot** — free with GitHub account\n"
            "2. **Anthropic** — Claude API\n"
            "3. **OpenAI** — GPT API\n"
            "4. **Custom (Anthropic-compatible)** — third-party Anthropic API\n"
            "5. **Custom (OpenAI-compatible)** — third-party OpenAI API\n\n"
            "Type a number to continue."
        )

    def _model_list_text(self, models: list[str]) -> str:
        """Build numbered model list text for chat display."""
        lines = ["Available models:\n"]
        shown = models[:_MAX_NUMBERED_MODELS]
        for i, m in enumerate(shown, 1):
            suffix = " (recommended)" if i == 1 else ""
            lines.append(f"{i}. **{m}**{suffix}")

        if len(models) > _MAX_NUMBERED_MODELS:
            lines.append(f"\nType **a** to see all {len(models)} models.")

        lines.append(
            "\nSelect models (type numbers separated by commas, "
            "or **all** to select all):"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------

    async def _dispatch(self, user_input: str) -> AsyncIterator[StreamEvent]:
        if self._state == "WELCOME":
            self._state = "AWAIT_CHOICE"
            async for e in self._reply(
                "👋 Welcome to MutBot! Let's set up your AI provider.\n\n"
                + self._choice_text()
            ):
                yield e
            return

        if self._state == "AWAIT_CHOICE":
            async for e in self._handle_choice(user_input):
                yield e
            return

        if self._state == "AWAIT_KEY":
            async for e in self._handle_api_key(user_input):
                yield e
            return

        if self._state == "AWAIT_CUSTOM_URL":
            async for e in self._handle_custom_url(user_input):
                yield e
            return

        if self._state == "AWAIT_CUSTOM_KEY":
            async for e in self._handle_custom_key(user_input):
                yield e
            return

        if self._state == "AWAIT_MODEL":
            async for e in self._handle_model_selection(user_input):
                yield e
            return

        if self._state == "AWAIT_MANUAL_MODEL":
            async for e in self._handle_manual_model(user_input):
                yield e
            return

        if self._state == "COPILOT_POLLING":
            # 轮询被取消后重新进入
            self._state = "AWAIT_CHOICE"
            async for e in self._reply(
                "Authorization was interrupted.\n\n" + self._choice_text()
            ):
                yield e
            return

        # fallback
        async for e in self._reply(
            "Something went wrong. Type **restart** to start over."
        ):
            yield e

    # ------------------------------------------------------------------
    # Provider 选择
    # ------------------------------------------------------------------

    async def _handle_choice(self, user_input: str) -> AsyncIterator[StreamEvent]:
        choice = user_input.lower().strip()

        if choice in ("1", "copilot"):
            async for e in self._do_copilot_auth():
                yield e
            return

        if choice in ("2", "anthropic"):
            self._state = "AWAIT_KEY"
            self._context["provider_type"] = "anthropic"
            async for e in self._reply("Please enter your Anthropic API key:"):
                yield e
            return

        if choice in ("3", "openai"):
            self._state = "AWAIT_KEY"
            self._context["provider_type"] = "openai"
            async for e in self._reply("Please enter your OpenAI API key:"):
                yield e
            return

        if choice in ("4",):
            self._state = "AWAIT_CUSTOM_URL"
            self._context["protocol"] = "anthropic"
            async for e in self._reply(
                "Enter the Anthropic-compatible API base URL:\n\n"
                "Example: `https://api.example.com`"
            ):
                yield e
            return

        if choice in ("5",):
            self._state = "AWAIT_CUSTOM_URL"
            self._context["protocol"] = "openai"
            async for e in self._reply(
                "Enter the OpenAI-compatible API base URL:\n\n"
                "Example: `https://api.example.com/v1`"
            ):
                yield e
            return

        async for e in self._reply("Please type a number (1-5) to select a provider."):
            yield e

    # ------------------------------------------------------------------
    # Copilot OAuth — 自动轮询
    # ------------------------------------------------------------------

    async def _do_copilot_auth(self) -> AsyncIterator[StreamEvent]:
        """Copilot OAuth Device Flow — 全流程在一次 send() 内完成。"""
        # 1. 请求 device code
        try:
            device_data = await self._request_device_code()
        except Exception as exc:
            logger.warning("Device code request failed: %s", exc)
            self._state = "AWAIT_CHOICE"
            async for e in self._reply(
                f"Failed to start GitHub authentication: {exc}\n\n"
                + self._choice_text()
            ):
                yield e
            return

        verification_uri = device_data["verification_uri"]
        user_code = device_data["user_code"]
        device_code = device_data["device_code"]
        interval = device_data.get("interval", 5)

        # 2. 展示验证码
        code_text = (
            f"Great! Let's connect your GitHub account.\n\n"
            f"Please visit this URL and enter the code:\n\n"
            f"🔗 {verification_uri}\n"
            f"📋 Code: **{user_code}**\n\n"
            f"Waiting for authorization..."
        )
        yield StreamEvent(type="text_delta", text=code_text)

        # 3. 异步轮询（最多 5 分钟）
        self._state = "COPILOT_POLLING"
        token = None
        max_attempts = 300 // interval  # ~5 分钟

        for _ in range(max_attempts):
            await asyncio.sleep(interval)
            try:
                token = await self._poll_github_token(device_code)
            except Exception as exc:
                logger.warning("GitHub token poll error: %s", exc)
                break
            if token:
                break

        # 4. 结果
        if token:
            self._context["github_token"] = token
            # Copilot: 硬编码模型列表，直接激活（与 CLI 行为一致）
            self._context["selected_models"] = ["claude-sonnet-4", "gpt-4.1"]
            result_text = await self._activate(provider="copilot")
            yield StreamEvent(type="text_delta", text="\n\n" + result_text)
            full_text = code_text + "\n\n" + result_text
        else:
            self._state = "AWAIT_CHOICE"
            timeout_text = (
                "\n\nAuthorization timed out. "
                "Please choose a provider to try again.\n\n"
                + self._choice_text()
            )
            yield StreamEvent(type="text_delta", text=timeout_text)
            full_text = code_text + timeout_text

        yield StreamEvent(type="response_done", response=Response(
            message=Message(role="assistant", blocks=[TextBlock(text=full_text)]),
            stop_reason="end_turn",
        ))

    async def _request_device_code(self) -> dict:
        """请求 GitHub device code。"""
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://github.com/login/device/code",
                headers={"Accept": "application/json"},
                data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
            )
            resp.raise_for_status()
            return resp.json()

    async def _poll_github_token(self, device_code: str) -> str | None:
        """单次轮询 GitHub token。"""
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            data = resp.json()
            error = data.get("error")
            if error in ("authorization_pending", "slow_down"):
                return None
            if error:
                raise RuntimeError(f"OAuth error: {error}")
            return data.get("access_token")

    # ------------------------------------------------------------------
    # API Key 流程 (Standard Anthropic / OpenAI)
    # ------------------------------------------------------------------

    async def _handle_api_key(self, user_input: str) -> AsyncIterator[StreamEvent]:
        if user_input.lower() == "cancel":
            self._state = "AWAIT_CHOICE"
            self._context.clear()
            async for e in self._reply("No problem.\n\n" + self._choice_text()):
                yield e
            return

        provider_type = self._context["provider_type"]
        key = user_input.strip()
        self._context["auth_token"] = key

        if provider_type == "anthropic":
            # Anthropic: 硬编码模型列表（与 CLI 行为一致）
            models = ["claude-sonnet-4", "claude-haiku-4.5", "claude-opus-4"]
            self._context["available_models"] = models
            self._state = "AWAIT_MODEL"
            async for e in self._reply(self._model_list_text(models)):
                yield e
        else:
            # OpenAI: 动态发现模型（fetch 即验证）
            base_url = "https://api.openai.com/v1"
            self._context["base_url"] = base_url
            models = await self._fetch_models_async(
                base_url, key, chat_filter=True,
            )
            note = ""
            if not models:
                models = ["gpt-4.1", "gpt-4.1-mini", "o3"]
                note = (
                    "Could not fetch models from OpenAI API. "
                    "Using default model list.\n\n"
                )
            self._context["available_models"] = models
            self._state = "AWAIT_MODEL"
            async for e in self._reply(note + self._model_list_text(models)):
                yield e

    # ------------------------------------------------------------------
    # Custom API 流程
    # ------------------------------------------------------------------

    async def _handle_custom_url(self, user_input: str) -> AsyncIterator[StreamEvent]:
        if user_input.lower() == "cancel":
            self._state = "AWAIT_CHOICE"
            self._context.clear()
            async for e in self._reply("No problem.\n\n" + self._choice_text()):
                yield e
            return

        url = user_input.strip()

        if not url or not url.startswith("http"):
            async for e in self._reply(
                "Please enter a valid URL starting with `http://` or `https://`.\n"
                "Type **cancel** to go back."
            ):
                yield e
            return

        self._context["base_url"] = url
        self._state = "AWAIT_CUSTOM_KEY"

        async for e in self._reply("Please enter your API key:"):
            yield e

    async def _handle_custom_key(self, user_input: str) -> AsyncIterator[StreamEvent]:
        if user_input.lower() == "cancel":
            self._state = "AWAIT_CHOICE"
            self._context.clear()
            async for e in self._reply("No problem.\n\n" + self._choice_text()):
                yield e
            return

        key = user_input.strip()
        base_url = self._context["base_url"]
        protocol = self._context["protocol"]
        self._context["auth_token"] = key

        chat_filter = (protocol == "openai")
        models = await self._fetch_models_async(
            base_url, key, chat_filter=chat_filter,
        )

        if models:
            self._context["available_models"] = models
            self._state = "AWAIT_MODEL"
            async for e in self._reply(self._model_list_text(models)):
                yield e
        else:
            self._state = "AWAIT_MANUAL_MODEL"
            async for e in self._reply(
                "Could not fetch models from the API. "
                "Please enter a model ID manually:\n\n"
                "Example: `claude-sonnet-4` or `gpt-4.1`"
            ):
                yield e

    # ------------------------------------------------------------------
    # Model 选择
    # ------------------------------------------------------------------

    async def _handle_model_selection(
        self, user_input: str,
    ) -> AsyncIterator[StreamEvent]:
        if user_input.lower() == "cancel":
            self._state = "AWAIT_CHOICE"
            self._context.clear()
            async for e in self._reply("No problem.\n\n" + self._choice_text()):
                yield e
            return

        models = self._context.get("available_models", [])
        text = user_input.strip()

        # "a" → 展示全部模型
        if text.lower() == "a" and len(models) > _MAX_NUMBERED_MODELS:
            lines = [f"All {len(models)} models:\n"]
            for i, m in enumerate(models, 1):
                lines.append(f"{i}. **{m}**")
            lines.append(
                "\nSelect models (type numbers separated by commas, "
                "or **all** to select all):"
            )
            self._context["show_all"] = True
            async for e in self._reply("\n".join(lines)):
                yield e
            return

        # "all" → 选择全部
        if text.lower() == "all":
            selected = list(models)
        else:
            # 解析逗号分隔的编号/名称
            shown = (
                models if self._context.get("show_all")
                else models[:_MAX_NUMBERED_MODELS]
            )
            selected = []
            for part in text.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    idx = int(part)
                    if 1 <= idx <= len(shown):
                        selected.append(shown[idx - 1])
                        continue
                except ValueError:
                    pass
                # 直接作为模型名
                selected.append(part)

            # 去重保持顺序
            seen: set[str] = set()
            unique: list[str] = []
            for m in selected:
                if m not in seen:
                    unique.append(m)
                    seen.add(m)
            selected = unique

        if not selected:
            async for e in self._reply("Please select at least one model."):
                yield e
            return

        self._context["selected_models"] = selected
        provider_type = self._context.get("provider_type")
        if provider_type:
            result = await self._activate(provider=provider_type)
        else:
            result = await self._activate(provider="custom")
        async for e in self._reply(result):
            yield e

    async def _handle_manual_model(
        self, user_input: str,
    ) -> AsyncIterator[StreamEvent]:
        if user_input.lower() == "cancel":
            self._state = "AWAIT_CHOICE"
            self._context.clear()
            async for e in self._reply("No problem.\n\n" + self._choice_text()):
                yield e
            return

        model_id = user_input.strip()
        if not model_id:
            async for e in self._reply("Please enter a model ID."):
                yield e
            return

        self._context["selected_models"] = [model_id]
        result = await self._activate(provider="custom")
        async for e in self._reply(result):
            yield e

    # ------------------------------------------------------------------
    # Model discovery (async)
    # ------------------------------------------------------------------

    async def _fetch_models_async(
        self,
        base_url: str,
        api_key: str,
        *,
        chat_filter: bool = False,
    ) -> list[str]:
        """调用 /models 或 /v1/models 端点获取模型列表。

        尝试 OpenAI 格式端点。返回按 family 优先级排序的模型 ID 列表，
        失败返回空列表（由调用方决定 fallback 策略）。
        """
        import httpx

        headers = {"Authorization": f"Bearer {api_key}"}
        urls = [f"{base_url}/models", f"{base_url}/v1/models"]
        data = None

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for url in urls:
                    try:
                        resp = await client.get(url, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            break
                    except Exception:
                        continue
        except Exception as exc:
            logger.warning("Model fetch failed for %s: %s", base_url, exc)
            return []

        if data is None:
            return []

        raw_models: list[tuple[str, int]] = []
        for item in data.get("data", []):
            model_id = item.get("id", "")
            if model_id:
                created = item.get("created", 0)
                raw_models.append((model_id, created))

        if not raw_models:
            return []

        if chat_filter:
            filtered = [
                (m, c) for m, c in raw_models
                if any(m.startswith(p) for p in _CHAT_MODEL_PREFIXES)
            ]
            if filtered:
                raw_models = filtered

        return _prioritize_models(raw_models)

    # ------------------------------------------------------------------
    # 配置保存与 Provider 切换
    # ------------------------------------------------------------------

    async def _activate(self, provider: str) -> str:
        """保存配置并切换到真实 LLM provider。"""
        config_data = self._build_provider_config(provider)
        _write_config(config_data)

        # 创建真实 LLMProvider — 后续 send() 直接代理
        from mutbot.runtime.session_impl import create_llm_client
        from mutbot.runtime.config import load_mutbot_config
        config = load_mutbot_config()
        client = create_llm_client(config)
        self._real_provider = client.provider
        self._real_model = client.model

        selected = self._context.get("selected_models", [])
        models_str = ", ".join(selected) if selected else self._real_model

        config_path = str(MUTBOT_CONFIG_PATH)
        return (
            f"✅ Configuration complete! "
            f"Using **{self._real_model}** as default model.\n"
            f"Selected models: {models_str}\n\n"
            f"📁 Config saved to: `{config_path}`\n"
            f"You can edit this file manually to adjust settings.\n\n"
            f"You can now chat with me — I'm powered by a real AI! "
            f"Try saying something to test the connection."
        )

    def _build_provider_config(self, provider: str) -> dict:
        """根据 provider 类型构建配置 dict。"""
        selected = self._context.get("selected_models", [])

        if provider == "copilot":
            github_token = self._context["github_token"]
            models = selected or ["claude-sonnet-4", "gpt-4.1"]
            return {
                "default_model": models[0],
                "providers": {
                    "copilot": {
                        "provider": "mutbot.copilot.provider.CopilotProvider",
                        "github_token": github_token,
                        "models": models,
                    },
                },
            }

        if provider == "anthropic":
            key = self._context["auth_token"]
            models = selected or ["claude-sonnet-4", "claude-haiku-4.5"]
            return {
                "default_model": models[0],
                "providers": {
                    "anthropic": {
                        "provider": "AnthropicProvider",
                        "base_url": "https://api.anthropic.com",
                        "auth_token": key,
                        "models": models,
                    },
                },
            }

        if provider == "openai":
            key = self._context["auth_token"]
            models = selected or ["gpt-4.1", "gpt-4.1-mini"]
            return {
                "default_model": models[0],
                "providers": {
                    "openai": {
                        "provider": "OpenAIProvider",
                        "base_url": "https://api.openai.com/v1",
                        "auth_token": key,
                        "models": models,
                    },
                },
            }

        # custom
        base_url = self._context["base_url"]
        key = self._context["auth_token"]
        protocol = self._context.get("protocol", "openai")

        if protocol == "anthropic":
            provider_cls = "AnthropicProvider"
        else:
            provider_cls = "OpenAIProvider"

        models = selected or []
        return {
            "default_model": models[0] if models else "",
            "providers": {
                "custom": {
                    "provider": provider_cls,
                    "base_url": base_url,
                    "auth_token": key,
                    "models": models,
                },
            },
        }


# ---------------------------------------------------------------------------
# Config I/O (module-level, testable)
# ---------------------------------------------------------------------------

def _write_config(new_data: dict) -> None:
    """合并写入 ~/.mutbot/config.json。

    - providers: 已有保留，同名覆盖
    - default_model: 仅在已有配置没有时设置
    """
    MUTBOT_USER_DIR.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if MUTBOT_CONFIG_PATH.exists():
        try:
            existing = json.loads(MUTBOT_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # 合并 providers
    existing_providers = existing.get("providers", {})
    new_providers = new_data.get("providers", {})
    existing_providers.update(new_providers)
    existing["providers"] = existing_providers

    # default_model: 始终更新为新配置的值
    if "default_model" in new_data:
        existing["default_model"] = new_data["default_model"]

    MUTBOT_CONFIG_PATH.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("Config written to %s", MUTBOT_CONFIG_PATH)
