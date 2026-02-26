"""mutbot.cli.setup -- 首次启动配置向导。

当 mutbot 检测到没有任何 provider 配置时，在终端启动交互式向导，
引导用户选择 LLM 提供商并完成配置，写入 ~/.mutbot/config.json。
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from mutbot.runtime.config import MUTBOT_USER_DIR

MUTBOT_CONFIG_PATH = MUTBOT_USER_DIR / "config.json"

# 向导中编号列表最多显示的模型数量
_MAX_NUMBERED_MODELS = 10

# chat 模型前缀过滤（用于 OpenAI /v1/models 响应）
_CHAT_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")

# 每个前缀组保留的最新 family 数量
_FEATURED_FAMILIES_PER_PREFIX = 2

# 模型变体后缀（用于 family 分组）
_VARIANT_SUFFIXES = ("-mini", "-nano", "-turbo", "-latest", "-preview", "-realtime")


def run_setup_wizard() -> None:
    """交互式配置向导入口。"""
    print("\nNo LLM providers configured. Let's set one up.\n")
    print("Select a provider:")
    print("  [1] GitHub Copilot (free with GitHub account)")
    print("  [2] Anthropic (Claude)")
    print("  [3] OpenAI")
    print("  [4] Other Anthropic-compatible API")
    print("  [5] Other OpenAI-compatible API")

    choice = _prompt_choice(5)

    if choice == 1:
        config_data = _setup_copilot()
    elif choice == 2:
        config_data = _setup_anthropic()
    elif choice == 3:
        config_data = _setup_openai()
    elif choice == 4:
        config_data = _setup_other_anthropic()
    else:
        config_data = _setup_other_openai()

    _write_config(config_data)


# ---------------------------------------------------------------------------
# Provider setup flows
# ---------------------------------------------------------------------------

def _setup_copilot() -> dict:
    """GitHub Copilot 设置流程（OAuth 设备流）。"""
    print("\nStarting GitHub Copilot authentication...")

    from mutbot.copilot.auth import CopilotAuth
    auth = CopilotAuth()
    auth.ensure_authenticated()

    github_token = auth.github_token
    print("\nAuthentication successful!")

    # 硬编码已知可用模型
    known_models = ["claude-sonnet-4", "gpt-4.1"]

    provider_conf = {
        "provider": "mutbot.copilot.provider.CopilotProvider",
        "github_token": github_token,
        "models": known_models,
    }

    models_str = ", ".join(known_models)
    print(f"\n  Provider: copilot")
    print(f"  Models: {models_str}")
    print(f"  Default: {known_models[0]}")

    return {
        "default_model": known_models[0],
        "providers": {"copilot": provider_conf},
    }


def _setup_anthropic() -> dict:
    """Anthropic 设置流程。"""
    api_key, use_env_ref = _prompt_api_key("ANTHROPIC_API_KEY")

    # 硬编码模型列表
    known_models = ["claude-sonnet-4", "claude-haiku-4.5", "claude-opus-4"]
    selected = _prompt_model_selection(known_models)

    token_value = "$ANTHROPIC_API_KEY" if use_env_ref else api_key
    provider_conf = {
        "provider": "AnthropicProvider",
        "base_url": "https://api.anthropic.com",
        "auth_token": token_value,
        "models": selected,
    }

    models_str = ", ".join(selected)
    print(f"\n  Provider: anthropic")
    print(f"  Models: {models_str}")
    print(f"  Default: {selected[0]}")

    return {
        "default_model": selected[0],
        "providers": {"anthropic": provider_conf},
    }


def _setup_openai() -> dict:
    """OpenAI 设置流程。"""
    api_key, use_env_ref = _prompt_api_key("OPENAI_API_KEY")
    token_value = "$OPENAI_API_KEY" if use_env_ref else api_key

    # 尝试动态发现模型
    models = _fetch_models(
        "https://api.openai.com/v1", api_key, chat_filter=True,
    )
    if not models:
        # fallback 到硬编码
        models = ["gpt-4.1", "gpt-4.1-mini", "o3"]

    selected = _prompt_model_selection(models)

    provider_conf = {
        "provider": "OpenAIProvider",
        "base_url": "https://api.openai.com/v1",
        "auth_token": token_value,
        "models": selected,
    }

    models_str = ", ".join(selected)
    print(f"\n  Provider: openai")
    print(f"  Models: {models_str}")
    print(f"  Default: {selected[0]}")

    return {
        "default_model": selected[0],
        "providers": {"openai": provider_conf},
    }


def _setup_other_anthropic() -> dict:
    """自定义 Anthropic 兼容 API 设置流程。"""
    print()
    base_url = input("Base URL (e.g. https://api.example.com): ").strip()
    api_key = input("API key: ").strip()

    if not base_url or not api_key:
        print("Error: base_url and API key are required.", file=sys.stderr)
        sys.exit(1)

    # 尝试动态发现模型
    models = _fetch_models(base_url, api_key)

    if models:
        selected = _prompt_model_selection(models)
    else:
        model_id = input("Model ID (e.g. claude-sonnet-4): ").strip()
        if not model_id:
            print("Error: model ID is required.", file=sys.stderr)
            sys.exit(1)
        selected = [model_id]

    provider_name = input("Provider name (default: custom): ").strip() or "custom"

    provider_conf = {
        "base_url": base_url,
        "auth_token": api_key,
        "models": selected,
    }

    models_str = ", ".join(selected)
    print(f"\n  Provider: {provider_name}")
    print(f"  Models: {models_str}")
    print(f"  Default: {selected[0]}")

    return {
        "default_model": selected[0],
        "providers": {provider_name: provider_conf},
    }


def _setup_other_openai() -> dict:
    """自定义 OpenAI 兼容 API 设置流程。"""
    print()
    base_url = input("Base URL (e.g. https://api.example.com/v1): ").strip()
    api_key = input("API key: ").strip()

    if not base_url or not api_key:
        print("Error: base_url and API key are required.", file=sys.stderr)
        sys.exit(1)

    # 尝试动态发现模型
    models = _fetch_models(base_url, api_key, chat_filter=True)

    if models:
        selected = _prompt_model_selection(models)
    else:
        # 手动输入
        model_id = input("Model ID (e.g. my-model): ").strip()
        if not model_id:
            print("Error: model ID is required.", file=sys.stderr)
            sys.exit(1)
        selected = [model_id]

    provider_conf = {
        "provider": "OpenAIProvider",
        "base_url": base_url,
        "auth_token": api_key,
        "models": selected,
    }

    provider_name = input("Provider name (default: custom): ").strip() or "custom"

    models_str = ", ".join(selected)
    print(f"\n  Provider: {provider_name}")
    print(f"  Models: {models_str}")
    print(f"  Default: {selected[0]}")

    return {
        "default_model": selected[0],
        "providers": {provider_name: provider_conf},
    }


# ---------------------------------------------------------------------------
# Model discovery & prioritization
# ---------------------------------------------------------------------------

def _model_family(name: str) -> str:
    """提取模型 family（去掉变体后缀）。

    gpt-4.1-mini → gpt-4.1, o3-mini → o3
    """
    for suffix in _VARIANT_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _major_prefix(family: str) -> str:
    """提取 family 的主前缀用于分组。

    gpt-4.1 → gpt, o3 → o, chatgpt-4o → chatgpt
    """
    m = re.match(r'^([a-zA-Z]+)', family)
    return m.group(1) if m else family


def _prioritize_models(models_with_ts: list[tuple[str, int]]) -> list[str]:
    """按 family 分组，每个前缀保留最新 N 个 family，其余排后面。

    Args:
        models_with_ts: (model_id, created_timestamp) 列表。

    Returns:
        重排后的模型 ID 列表：featured families 在前，其余在后。
    """
    # 按 family 分组
    families: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for model_id, created in models_with_ts:
        fam = _model_family(model_id)
        families[fam].append((model_id, created))

    # 每个 family 的最新时间戳
    family_recency: dict[str, int] = {
        fam: max(c for _, c in members)
        for fam, members in families.items()
    }

    # 按主前缀分组 families
    prefix_families: dict[str, list[str]] = defaultdict(list)
    for fam in families:
        prefix = _major_prefix(fam)
        prefix_families[prefix].append(fam)

    # 每个前缀取最新 N 个 family
    featured_set: set[str] = set()
    for _prefix, fams in prefix_families.items():
        fams_sorted = sorted(fams, key=lambda f: family_recency[f], reverse=True)
        for f in fams_sorted[:_FEATURED_FAMILIES_PER_PREFIX]:
            featured_set.add(f)

    # 所有 family 按时间倒序
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


def _fetch_models(
    base_url: str, api_key: str, *, chat_filter: bool = False,
) -> list[str]:
    """调用 /v1/models 或 /models 端点获取模型列表。

    尝试 OpenAI 格式（{base_url}/models）和 Anthropic 格式（{base_url}/v1/models）。
    返回按 family 优先级排序的模型 ID 列表，失败返回空列表。

    Args:
        chat_filter: 为 True 时按前缀过滤 chat 类模型（适用于 OpenAI）。
    """
    import requests

    print("\nFetching available models...")
    headers = {"Authorization": f"Bearer {api_key}"}

    # 尝试多个端点路径
    urls = [f"{base_url}/models", f"{base_url}/v1/models"]
    data = None
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                break
        except Exception:
            continue

    if data is None:
        print("  Could not fetch models (connection error or unsupported)")
        return []

    raw_models: list[tuple[str, int]] = []
    for item in data.get("data", []):
        model_id = item.get("id", "")
        if model_id:
            created = item.get("created", 0)
            raw_models.append((model_id, created))

    if not raw_models:
        print("  No models found")
        return []

    if chat_filter:
        filtered = [
            (m, c) for m, c in raw_models
            if any(m.startswith(p) for p in _CHAT_MODEL_PREFIXES)
        ]
        if filtered:
            raw_models = filtered

    return _prioritize_models(raw_models)


# ---------------------------------------------------------------------------
# Model selection UI
# ---------------------------------------------------------------------------

def _prompt_model_selection(models: list[str]) -> list[str]:
    """让用户从模型列表中多选。

    显示编号列表（最多 _MAX_NUMBERED_MODELS 个），有更多模型时提供 [a] 展开全部。
    支持编号和模型名称混合输入，逗号分隔。
    """
    numbered = models[:_MAX_NUMBERED_MODELS]
    has_overflow = len(models) > _MAX_NUMBERED_MODELS

    print("\nAvailable models:")
    for i, m in enumerate(numbered, 1):
        suffix = " (recommended)" if i == 1 else ""
        print(f"  [{i}] {m}{suffix}")

    if has_overflow:
        print(f"  [a] Show all {len(models)} models")

    # 只有一个模型时直接选中
    if len(models) == 1:
        print(f"\n  Auto-selected: {models[0]}")
        return models

    print("\nSelect models (numbers or names, comma-separated):")

    current_numbered = numbered
    showing_all = False

    while True:
        raw = input("> ").strip()
        if not raw:
            continue

        # 处理 "show all" 命令
        if raw.lower() == "a" and has_overflow and not showing_all:
            showing_all = True
            current_numbered = models
            print(f"\nAll {len(models)} models:")
            for i, m in enumerate(models, 1):
                print(f"  [{i}] {m}")
            print("\nSelect models (numbers or names, comma-separated):")
            continue

        selected: list[str] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            # 尝试解析为编号
            try:
                idx = int(part)
                if 1 <= idx <= len(current_numbered):
                    selected.append(current_numbered[idx - 1])
                    continue
            except ValueError:
                pass
            # 直接作为模型名（不验证是否在列表中）
            selected.append(part)

        if selected:
            # 去重保持顺序
            seen: set[str] = set()
            unique: list[str] = []
            for m in selected:
                if m not in seen:
                    unique.append(m)
                    seen.add(m)
            return unique

        print("  Please select at least one model.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prompt_choice(max_choice: int) -> int:
    """提示用户选择编号（1-based）。"""
    while True:
        try:
            raw = input("\n> ").strip()
            n = int(raw)
            if 1 <= n <= max_choice:
                return n
        except (ValueError, EOFError):
            pass
        print(f"Please enter a number between 1 and {max_choice}.")


def _prompt_api_key(env_var: str) -> tuple[str, bool]:
    """提示输入 API key，优先从环境变量自动填充。

    Returns:
        (api_key, use_env_ref): api_key 是实际值，use_env_ref 表示配置中是否用 $VAR 引用。
    """
    env_value = os.environ.get(env_var)
    if env_value:
        masked = env_value[:7] + "***" + env_value[-3:]
        print(f"\nDetected {env_var} from environment.")
        print(f"API key: {masked} (from ${env_var})")
        confirm = input("Use this key? [Y/n] ").strip().lower()
        if confirm in ("", "y", "yes"):
            return env_value, True

    api_key = input(f"\nAPI key: ").strip()
    if not api_key:
        print("Error: API key is required.", file=sys.stderr)
        sys.exit(1)
    return api_key, False


def _write_config(new_data: dict) -> None:
    """写入配置到 ~/.mutbot/config.json，与已有配置合并。"""
    MUTBOT_USER_DIR.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if MUTBOT_CONFIG_PATH.exists():
        try:
            existing = json.loads(MUTBOT_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # 合并 providers（已有的保留，同名 provider 覆盖）
    existing_providers = existing.get("providers", {})
    new_providers = new_data.get("providers", {})
    existing_providers.update(new_providers)
    existing["providers"] = existing_providers

    # 设置 default_model（仅在没有时设置）
    if "default_model" not in existing and "default_model" in new_data:
        existing["default_model"] = new_data["default_model"]

    MUTBOT_CONFIG_PATH.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nConfig written to {MUTBOT_CONFIG_PATH}")
