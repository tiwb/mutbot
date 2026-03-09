"""mutbot.builtins.config_toolkit -- 配置管理工具集。

提供 LLM provider 配置向导（Config-llm）和通用配置修改（Config-update）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from typing import Any, AsyncGenerator, AsyncIterator
from uuid import uuid4

from mutagent.config import Config
from mutagent.messages import Message, Response, StreamEvent, TextBlock, ToolSchema, ToolUseBlock
from mutagent.provider import LLMProvider
from mutbot.ui.toolkit import UIToolkitBase

logger = logging.getLogger(__name__)

# VS Code Copilot Chat 使用的 Client ID（与 auth.py 一致）
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"

# --- Model discovery constants (ported from CLI setup.py) ---
_MAX_NUMBERED_MODELS = 10
_CHAT_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
_FEATURED_FAMILIES_PER_PREFIX = 2
_VARIANT_SUFFIXES = ("-mini", "-nano", "-turbo", "-latest", "-preview", "-realtime")


# ---------------------------------------------------------------------------
# NullProvider — 无 LLM 配置时占位
# ---------------------------------------------------------------------------

class NullProvider(LLMProvider):
    """占位 LLM Provider — 无 LLM 配置时满足 Agent 构造要求。

    无论用户发什么消息，都返回引导文本 + Config-llm tool_use，
    让 Agent 自动进入配置流程。
    配置完成后由 ConfigToolkit._activate() 直接替换 agent.llm。
    """

    @classmethod
    def from_spec(cls, spec: dict) -> NullProvider:
        return cls()

    async def send(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolSchema],
        prompts: list[Message] | None = None,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        guide_text = (
            "Welcome to MutBot! No LLM provider is configured yet. "
            "Let me help you set one up."
        )
        yield StreamEvent(type="text_delta", text=guide_text)

        tool_block = ToolUseBlock(
            id="setup_" + uuid4().hex[:10],
            name="Config-llm",
            input={},
        )
        yield StreamEvent(type="response_done", response=Response(
            message=Message(role="assistant", blocks=[
                TextBlock(text=guide_text),
                tool_block,
            ]),
            stop_reason="tool_use",
        ))


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


# configure 方法返回的三元组类型
_ConfigResult = tuple[str, dict[str, Any], list[str]]  # (provider_key, config, models)

# Provider 类型定义：(选项 value, 标签, 协议, 默认 base_url, Provider 类名)
_PROVIDER_DEFS = {
    "copilot": {
        "label": "GitHub Copilot",
        "protocol": "copilot",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "protocol": "anthropic",
        "default_url": "https://api.anthropic.com",
        "provider_cls": "AnthropicProvider",
        "default_models": ["claude-sonnet-4", "claude-haiku-4.5", "claude-opus-4"],
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "protocol": "openai",
        "default_url": "https://api.openai.com/v1",
        "provider_cls": "OpenAIProvider",
        "default_models": ["gpt-4.1", "gpt-4.1-mini", "o3"],
    },
}


class ConfigToolkit(UIToolkitBase):
    """配置管理工具集 — LLM 配置向导 + 通用配置修改。

    通过 UIContext 在 ToolCallCard 内渲染交互式配置表单。
    """

    _tool_methods = ["llm", "update"]

    @property
    def _config(self) -> Config:
        """共享 Config 实例，通过 Agent 传递链获取。"""
        assert self.owner is not None, "ConfigToolkit.owner not bound"
        assert self.owner.agent is not None, "ConfigToolkit.owner.agent not bound"
        return self.owner.agent.config

    async def llm(self) -> str:
        """LLM provider 配置管理。

        支持查看/添加/编辑/删除 provider。添加后立即保存。
        首次使用（无已有配置）直接进入添加流程。
        """
        config = self._config
        providers = config.get("providers", default={}) or {}

        # 首次使用 → 直接添加
        if not providers:
            result = await self._add_provider_flow()
            if result is None:
                return "Configuration cancelled."
            key, pconf, models = result
            self._save_provider(key, pconf)

        # 主循环：Provider 列表页
        while True:
            action, data = await self._show_provider_list()
            selected_pkey = data.get("selected_provider", "")
            if action == "done":
                new_default = data.get("default_model", "")
                if new_default:
                    config.set("default_model", new_default)
                break
            elif action == "add":
                result = await self._add_provider_flow()
                if result is not None:
                    key, pconf, models = result
                    self._save_provider(key, pconf)
            elif action == "edit":
                if selected_pkey:
                    await self._edit_provider(selected_pkey)

        # 验证还有 provider
        providers = config.get("providers", default={}) or {}
        if not providers:
            return "All providers removed. No configuration saved."

        # 确保 default_model 有效
        default_model = config.get("default_model", default="") or ""
        all_model_ids: list[str] = []
        for pconf in providers.values():
            all_model_ids.extend(pconf.get("models", []))
        if default_model not in all_model_ids and all_model_ids:
            default_model = all_model_ids[0]
            config.set("default_model", default_model)

        return self._activate()

    async def update(self, view: dict) -> dict:
        """Update configuration via a UI form.

        View format is the same as UI-show (title, components, actions), with these rules:
        - Component id = config key (e.g. "WebToolkit.jina_api_key")
        - Components with id starting with __ are display-only, not saved to config
        - Value backfill: if you provide a value, it is used as-is; if omitted,
          the current config value is shown to the user automatically
        - Returns {"updated": ["key1", ...], "cancelled": true/false} — never includes
          the actual values the user entered

        Args:
            view: Declarative View whose component ids are config keys.
        """
        # 防御：LLM 可能传 JSON 字符串而非 dict
        if isinstance(view, str):
            import json
            view = json.loads(view)

        config = self._config
        components = view.get("components", [])

        # 值回填：Bot 未提供 value 的配置组件，从 config 读取已有值
        for comp in components:
            cid = comp.get("id", "")
            if cid.startswith("__"):
                continue
            if "value" not in comp:
                existing = config.get(cid, default=None)
                if existing is not None:
                    comp["value"] = existing

        # 默认 actions
        if "actions" not in view:
            view["actions"] = [
                {"type": "cancel", "label": "Cancel"},
                {"type": "submit", "label": "Save", "primary": True},
            ]

        data = await self.ui.show(view)

        if data is None:
            return {"updated": [], "cancelled": True}

        # 写入 config，收集成功更新的 key
        updated: list[str] = []
        for comp in components:
            cid = comp.get("id", "")
            if cid.startswith("__"):
                continue
            value = data.get(cid)
            # 空字符串跳过
            if isinstance(value, str) and not value.strip():
                continue
            if value is not None:
                config.set(cid, value)
                updated.append(cid)

        return {"updated": updated, "cancelled": False}

    # ------------------------------------------------------------------
    # Provider 列表页
    # ------------------------------------------------------------------

    async def _show_provider_list(self) -> tuple[str, dict[str, Any]]:
        """显示已配置 provider 列表页。

        返回 (action, formData)。action: "edit" | "add" | "done"。
        点击 Provider 直接进入编辑页面（auto_submit）。
        """
        config = self._config
        providers = config.get("providers", default={}) or {}
        default_model = config.get("default_model", default="") or ""

        components: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []

        if providers:
            # Provider 列表 — 点击直接编辑
            provider_options = []
            for pkey, pconf in providers.items():
                models = pconf.get("models", [])
                models_str = ", ".join(models[:3])
                if len(models) > 3:
                    models_str += f" (+{len(models) - 3})"
                provider_options.append({
                    "value": pkey,
                    "label": f"{pkey}  —  {models_str}",
                })
            components.append({
                "type": "select", "id": "selected_provider",
                "label": "Providers (click to edit)",
                "layout": "vertical",
                "auto_submit": True,
                "options": provider_options,
            })

            # Default model 选择
            all_models: list[dict[str, str]] = []
            for pkey, pconf in providers.items():
                for mid in pconf.get("models", []):
                    all_models.append({
                        "value": mid,
                        "label": f"{mid} ({pkey})",
                    })
            if all_models:
                components.append({
                    "type": "select", "id": "default_model",
                    "label": "Default Model",
                    "layout": "dropdown",
                    "options": all_models,
                    "value": default_model or all_models[0]["value"],
                })

            actions = [
                {"type": "add", "label": "Add"},
                {"type": "done", "label": "Done", "primary": True},
            ]
        else:
            # 无 provider 时只显示 Add
            actions = [
                {"type": "add", "label": "Add provider"},
            ]

        self.ui.set_view({
            "title": "LLM Configuration",
            "components": components,
            "actions": actions,
        })

        # 捕获所有事件类型
        event = await self.ui.wait_event()
        if event.type == "submit":
            # auto_submit 触发 → 编辑选中的 provider
            selected = event.data.get("selected_provider", "")
            if selected and selected in providers:
                return ("edit", {"selected_provider": selected, **event.data})
            return ("done", event.data)
        if event.type == "action":
            action_name = event.data.get("action", "")
            if action_name == "done":
                return ("done", event.data)
            return (action_name, event.data)
        return ("done", event.data)

    # ------------------------------------------------------------------
    # 添加 Provider 流程
    # ------------------------------------------------------------------

    async def _add_provider_flow(self) -> _ConfigResult | None:
        """选择 provider 类型并完成配置。返回 (key, config, models) 或 None。"""
        provider = await self._select_provider()
        if not provider:
            return None

        if provider == "copilot":
            result = await self._configure_copilot()
            if result is None:
                return None
            key, config, models = result
            # Copilot 固定 key
            return ("copilot", config, models)

        # Anthropic / OpenAI（带可编辑 base_url）
        return await self._configure_api_provider(provider)

    # ------------------------------------------------------------------
    # 编辑 Provider
    # ------------------------------------------------------------------

    async def _edit_provider(self, pkey: str) -> None:
        """编辑已有 provider：单页面同时展示 API Key 和 Models。

        包含 Delete 操作，删除成功后直接返回。
        """
        config = self._config
        pconf = (config.get(f"providers.{pkey}") or {}).copy()
        if not pconf:
            return

        provider_path = pconf.get("provider", "")
        is_copilot = "copilot" in provider_path.lower() or pkey == "copilot"
        current_models = list(pconf.get("models", []))

        # 初始获取可用模型
        available_models = list(current_models)
        fetch_error = ""
        loading = False
        # refresh 时暂存的变量
        effective_key = ""
        base_url = pconf.get("base_url", "")

        if is_copilot:
            token = pconf.get("github_token", "")
            if token:
                fetched = await self._fetch_copilot_models(token)
                if fetched:
                    seen = set(available_models)
                    for m in fetched:
                        if m not in seen:
                            available_models.append(m)
                            seen.add(m)
        else:
            api_key = pconf.get("auth_token", "")
            if base_url and api_key:
                chat_filter = "openai" in provider_path.lower()
                fetched = await self._fetch_models(base_url, api_key, chat_filter=chat_filter)
                if fetched:
                    seen = set(available_models)
                    for m in fetched:
                        if m not in seen:
                            available_models.append(m)
                            seen.add(m)

        while True:
            components: list[dict[str, Any]] = []

            # API Key 输入（Copilot 不显示）
            if not is_copilot:
                components.append({
                    "type": "text", "id": "api_key",
                    "label": "API Key (leave empty to keep current)",
                    "placeholder": "Enter new API key...",
                    "secret": True,
                })

            if loading:
                # 正在刷新：用 spinner 替代模型列表
                components.append({
                    "type": "spinner", "id": "loading",
                    "text": "Fetching models...",
                })
            else:
                # 模型多选
                # 构建 options：已选模型优先排列，确保不被截断
                selected_set = set(current_models)
                ordered = list(current_models)  # 已选的排前面
                for m in available_models:
                    if m not in selected_set:
                        ordered.append(m)
                options = [{"value": m, "label": m} for m in ordered]
                if fetch_error:
                    components.append({
                        "type": "badge", "id": "fetch_err",
                        "text": fetch_error, "variant": "warning",
                    })
                if options:
                    components.append({
                        "type": "select", "id": "models",
                        "label": "Models",
                        "layout": "vertical",
                        "multiple": True,
                        "scrollable": True,
                        "options": options,
                        "value": current_models,
                    })
                components.append({
                    "type": "text", "id": "custom_models",
                    "label": "Or enter additional model IDs (comma-separated)",
                    "placeholder": "e.g. my-model-1, my-model-2",
                })

            actions: list[dict[str, Any]] = [
                {"type": "cancel", "label": "Back"},
                {"type": "delete", "label": "Delete"},
            ]
            if not is_copilot and not loading:
                actions.append({"type": "refresh", "label": "Refresh Models"})
            if not loading:
                actions.append({"type": "submit", "label": "Save", "primary": True})

            self.ui.set_view({
                "title": f"Edit — {pkey}",
                "components": components,
                "actions": actions,
            })

            if loading:
                # 视图已显示 spinner，现在执行实际 fetch
                if is_copilot:
                    fetched = await self._fetch_copilot_models(
                        pconf.get("github_token", ""))
                else:
                    chat_filter = "openai" in provider_path.lower()
                    fetched = await self._fetch_models(
                        base_url, effective_key, chat_filter=chat_filter)

                loading = False
                if fetched:
                    available_models = list(fetched)
                    # 保留用户已选中的自定义模型
                    seen = set(available_models)
                    for m in current_models:
                        if m not in seen:
                            available_models.append(m)
                            seen.add(m)
                    fetch_error = ""
                else:
                    fetch_error = "Failed to fetch models. Check API key and URL."
                continue  # 重绘表单

            event = await self.ui.wait_event()

            if event.type == "cancel":
                return

            if event.type == "action" and event.data.get("action") == "delete":
                deleted = await self._delete_provider(pkey)
                if deleted:
                    return
                continue  # 取消删除 → 回到编辑页

            if event.type == "action" and event.data.get("action") == "refresh":
                # 从表单中提取用户当前选择（含自定义模型）
                form_selected = event.data.get("models", [])
                if isinstance(form_selected, str):
                    form_selected = [form_selected] if form_selected else []
                custom = (event.data.get("custom_models") or "").strip()
                if custom:
                    for m in custom.split(","):
                        m = m.strip()
                        if m and m not in form_selected:
                            form_selected.append(m)
                current_models = form_selected if form_selected else list(current_models)

                new_key = (event.data.get("api_key") or "").strip()
                effective_key = new_key or pconf.get("auth_token", "")
                base_url = pconf.get("base_url", "")

                if not is_copilot and not effective_key:
                    fetch_error = "No API key provided."
                    continue

                loading = True
                continue  # 下一轮循环先显示 loading 再 fetch

            # submit → Save
            form = event.data
            new_key = (form.get("api_key") or "").strip()
            if new_key and not is_copilot:
                pconf["auth_token"] = new_key

            selected = form.get("models", [])
            if isinstance(selected, str):
                selected = [selected] if selected else []
            custom = (form.get("custom_models") or "").strip()
            if custom:
                for m in custom.split(","):
                    m = m.strip()
                    if m and m not in selected:
                        selected.append(m)

            if selected:
                pconf["models"] = selected
            config.set(f"providers.{pkey}", pconf)
            return

    # ------------------------------------------------------------------
    # 删除 Provider
    # ------------------------------------------------------------------

    async def _delete_provider(self, pkey: str) -> bool:
        """从配置中删除指定 provider（需确认）。返回是否已删除。"""
        self.ui.set_view({
            "title": f"Delete \"{pkey}\"?",
            "components": [
                {
                    "type": "hint", "id": "warn",
                    "text": "This will remove the provider and its credentials. This cannot be undone.",
                },
            ],
            "actions": [
                {"type": "cancel", "label": "Back"},
                {"type": "submit", "label": "Delete", "primary": True},
            ],
        })
        event = await self.ui.wait_event()
        if event.type != "submit":
            return False

        existing = self._config.get("providers", default={}) or {}
        providers = dict(existing)
        if pkey in providers:
            del providers[pkey]
            self._config.set("providers", providers)
            logger.info("Deleted provider: %s", pkey)
        return True

    # ------------------------------------------------------------------
    # Provider 选择
    # ------------------------------------------------------------------

    async def _select_provider(self) -> str | None:
        """选择 LLM provider 类型，返回 provider 标识或 None（取消）。"""
        options = [
            {"value": k, "label": d["label"]}
            for k, d in _PROVIDER_DEFS.items()
        ]
        data = await self.ui.show({
            "title": "Add Provider",
            "components": [
                {
                    "type": "hint",
                    "id": "welcome",
                    "text": "Choose a provider to add.",
                },
                {
                    "type": "select",
                    "id": "provider",
                    "label": "Provider",
                    "layout": "vertical",
                    "auto_submit": True,
                    "options": options,
                },
            ],
        })
        if data is None:
            return None
        return data.get("provider")
    # ------------------------------------------------------------------
    # Copilot OAuth
    # ------------------------------------------------------------------

    async def _configure_copilot(self) -> _ConfigResult | None:
        """GitHub Copilot OAuth device flow。"""
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://github.com/login/device/code",
                    headers={"Accept": "application/json"},
                    data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
                )
                resp.raise_for_status()
                device_data = resp.json()
        except Exception as exc:
            logger.warning("Device code request failed: %s", exc)
            self.ui.set_view({
                "title": "Authentication Failed",
                "components": [
                    {"type": "badge", "id": "err", "text": "Error", "variant": "error"},
                    {"type": "hint", "id": "msg", "text": f"Failed to start GitHub auth: {exc}"},
                ],
                "actions": [{"type": "cancel", "label": "Back"}],
            })
            await self.ui.wait_event(type="cancel")
            return None

        verification_uri = device_data["verification_uri"]
        user_code = device_data["user_code"]
        device_code = device_data["device_code"]
        interval = device_data.get("interval", 5)

        self.ui.set_view({
            "title": "GitHub Authorization",
            "components": [
                {
                    "type": "hint", "id": "instructions",
                    "text": "Visit the link below and enter the code:",
                },
                {"type": "link", "id": "auth_link", "url": verification_uri, "label": "Open GitHub"},
                {"type": "copyable", "id": "code", "text": user_code},
                {"type": "spinner", "id": "polling", "text": "Waiting for authorization..."},
            ],
        })

        token = None
        max_attempts = 300 // interval

        for _ in range(max_attempts):
            await asyncio.sleep(interval)
            try:
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
                        continue
                    if error:
                        logger.warning("OAuth error: %s", error)
                        break
                    token = data.get("access_token")
                    if token:
                        break
            except Exception as exc:
                logger.warning("GitHub token poll error: %s", exc)
                break

        if not token:
            self.ui.set_view({
                "title": "Authorization Failed",
                "components": [
                    {"type": "badge", "id": "err", "text": "Timed out", "variant": "warning"},
                    {"type": "hint", "id": "msg", "text": "Authorization timed out or failed. Please try again."},
                ],
                "actions": [{"type": "cancel", "label": "Back"}],
            })
            await self.ui.wait_event(type="cancel")
            return None

        models = await self._fetch_copilot_models(token)
        if not models:
            models = ["claude-sonnet-4", "gpt-4.1"]

        selected = await self._select_provider_models(models, provider_name="copilot")
        if not selected:
            return None

        config: dict[str, Any] = {
            "provider": "mutbot.copilot.provider.CopilotProvider",
            "github_token": token,
            "models": selected,
        }
        return ("copilot", config, selected)

    # ------------------------------------------------------------------
    # API Provider（Anthropic / OpenAI，含可编辑 base_url）
    # ------------------------------------------------------------------

    async def _configure_api_provider(self, provider_type: str) -> _ConfigResult | None:
        """API Key + 可编辑 base_url 的统一配置流程。"""
        pdef = _PROVIDER_DEFS[provider_type]
        default_url = pdef["default_url"]
        provider_cls = pdef["provider_cls"]

        if provider_type == "anthropic":
            key_label = "Anthropic API Key"
            key_placeholder = "sk-ant-..."
        else:
            key_label = "OpenAI API Key"
            key_placeholder = "sk-..."

        data = await self.ui.show({
            "title": f"Configure {pdef['label']}",
            "components": [
                {
                    "type": "text", "id": "base_url",
                    "label": "API Base URL",
                    "value": default_url,
                },
                {
                    "type": "text", "id": "api_key",
                    "label": key_label,
                    "placeholder": key_placeholder,
                    "secret": True,
                },
                {
                    "type": "text", "id": "provider_name",
                    "label": "Provider Name",
                    "value": provider_type,
                    "placeholder": "e.g. my-claude, company-api",
                },
            ],
            "actions": [
                {"type": "cancel", "label": "Back"},
                {"type": "submit", "label": "Continue", "primary": True},
            ],
        })

        if data is None:
            return None
        api_key = data.get("api_key", "").strip()
        if not api_key:
            return None

        base_url = data.get("base_url", "").strip() or default_url
        provider_name = data.get("provider_name", "").strip() or provider_type

        # 获取模型列表
        chat_filter = (provider_type == "openai")
        if base_url == default_url and provider_type == "anthropic":
            # Anthropic 官方 API 不支持 /models 端点，用硬编码列表
            models = list(pdef["default_models"])
        else:
            models = await self._fetch_models(base_url, api_key, chat_filter=chat_filter)
            if not models:
                models = list(pdef.get("default_models", []))

        selected = await self._select_provider_models(models, provider_name=provider_name)
        if not selected:
            return None

        config: dict[str, Any] = {
            "provider": provider_cls,
            "base_url": base_url,
            "auth_token": api_key,
            "models": selected,
        }
        return (provider_name, config, selected)

    # ------------------------------------------------------------------
    # Provider 模型多选
    # ------------------------------------------------------------------

    async def _select_provider_models(
        self, models: list[str], *, preselected: list[str] | None = None,
        provider_name: str | None = None,
    ) -> list[str] | None:
        """多选该 provider 的模型。返回选中模型列表或 None（取消）。"""
        options = [{"value": m, "label": m} for m in models]
        default_selected = preselected if preselected else ([models[0]] if models else [])

        components: list[dict[str, Any]] = []
        if options:
            components.append({
                "type": "hint", "id": "note",
                "text": "Select the models you want to use (multiple OK).",
            })
            components.append({
                "type": "select", "id": "models",
                "label": "Models",
                "layout": "vertical",
                "multiple": True,
                "scrollable": True,
                "options": options,
                "value": default_selected,
            })
            components.append({
                "type": "text", "id": "custom_models",
                "label": "Or enter additional model IDs (comma-separated)",
                "placeholder": "e.g. my-model-1, my-model-2",
            })
        else:
            components.append({
                "type": "hint", "id": "note",
                "text": "Could not fetch models. Please enter model IDs manually.",
            })
            components.append({
                "type": "text", "id": "custom_models",
                "label": "Model IDs (comma-separated)",
                "placeholder": "e.g. claude-sonnet-4, gpt-4.1",
            })

        title = f"Select Models — {provider_name}" if provider_name else "Select Models"
        data = await self.ui.show({
            "title": title,
            "components": components,
            "actions": [
                {"type": "cancel", "label": "Back"},
                {"type": "submit", "label": "Continue", "primary": True},
            ],
        })

        if data is None:
            return None
        selected = data.get("models", [])
        if isinstance(selected, str):
            selected = [selected] if selected else []

        custom = data.get("custom_models", "").strip()
        if custom:
            for m in custom.split(","):
                m = m.strip()
                if m and m not in selected:
                    selected.append(m)

        return selected if selected else None

    # ------------------------------------------------------------------
    # Config 写入 helpers
    # ------------------------------------------------------------------

    def _save_provider(self, key: str, provider_config: dict[str, Any]) -> None:
        """保存单个 provider，同时设置默认模型（如果尚未设置）。"""
        config = self._config
        config.set(f"providers.{key}", provider_config)
        if not config.get("default_model"):
            models = provider_config.get("models", [])
            if models:
                config.set("default_model", models[0])
        logger.info("Saved provider: %s", key)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _fetch_copilot_models(self, github_token: str) -> list[str]:
        """通过 Copilot API 动态获取可用模型列表。"""
        import httpx
        from mutbot.copilot.auth import CopilotAuth

        try:
            auth = CopilotAuth.get_instance()
            auth.github_token = github_token
            auth._refresh_copilot_token()

            headers = auth.get_headers()
            base_url = auth.get_base_url()

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{base_url}/models",
                    headers=headers,
                )
                if resp.status_code != 200:
                    logger.warning("Copilot /models returned %d: %s",
                                   resp.status_code, resp.text[:200])
                    return []

                data = resp.json()
        except Exception as exc:
            logger.warning("Copilot model fetch failed: %s", exc)
            return []

        raw_models: list[tuple[str, int]] = []
        for item in data.get("data", data) if isinstance(data, dict) else data:
            model_id = item.get("id", "") if isinstance(item, dict) else ""
            if model_id:
                created = item.get("created", 0) if isinstance(item, dict) else 0
                raw_models.append((model_id, created))

        if not raw_models:
            return []

        return _prioritize_models(raw_models)

    async def _fetch_models(
        self, base_url: str, api_key: str, *, chat_filter: bool = False,
    ) -> list[str]:
        """从 API 获取模型列表。"""
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

    def _activate(self) -> str:
        """激活配置，切换到真实 LLM Provider。"""
        from mutbot.runtime.session_manager import create_llm_client

        config = self._config
        client = create_llm_client(config)

        assert self.owner is not None, "ConfigToolkit.owner not bound"
        assert self.owner.agent is not None, "ConfigToolkit.owner.agent not bound"
        agent = self.owner.agent
        agent.llm = client

        providers = config.get("providers", default={}) or {}
        all_models: list[str] = []
        for pconf in providers.values():
            all_models.extend(pconf.get("models", []))
        models_str = ", ".join(all_models)
        return (
            f"Configuration complete! Using {client.model} as default model. "
            f"Selected models: {models_str}. "
            f"You can now chat — try saying something!"
        )
