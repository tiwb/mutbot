"""mutbot.cli.setup -- 首次启动配置向导。

当 mutbot 检测到没有任何模型配置时，在终端启动交互式向导，
引导用户选择 LLM 提供商并完成配置，写入 ~/.mutbot/config.json。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from mutbot.runtime.config import MUTBOT_USER_DIR

MUTBOT_CONFIG_PATH = MUTBOT_USER_DIR / "config.json"


def run_setup_wizard() -> None:
    """交互式配置向导入口。"""
    print("\nNo LLM models configured. Let's set one up.\n")
    print("Select a provider:")
    print("  [1] GitHub Copilot (free with GitHub account)")
    print("  [2] Anthropic (Claude)")
    print("  [3] OpenAI")
    print("  [4] Other OpenAI-compatible API")

    choice = _prompt_choice(4)

    if choice == 1:
        config_data = _setup_copilot()
    elif choice == 2:
        config_data = _setup_anthropic()
    elif choice == 3:
        config_data = _setup_openai()
    else:
        config_data = _setup_other()

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

    # 写入配置时使用明文 token（单用户场景直接写入）
    models = {
        "copilot-claude": {
            "provider": "mutbot.copilot.provider.CopilotProvider",
            "model_id": "claude-sonnet-4",
            "github_token": github_token,
        },
        "copilot-gpt": {
            "provider": "mutbot.copilot.provider.CopilotProvider",
            "model_id": "gpt-4.1",
            "github_token": github_token,
        },
    }

    print(f"\n  Models: copilot-claude (claude-sonnet-4), copilot-gpt (gpt-4.1)")
    print(f"  Default: copilot-claude")

    return {"default_model": "copilot-claude", "models": models}


def _setup_anthropic() -> dict:
    """Anthropic 设置流程。"""
    api_key, use_env_ref = _prompt_api_key("ANTHROPIC_API_KEY")

    print("\nSelect model:")
    print("  [1] claude-sonnet-4 (recommended)")
    print("  [2] claude-haiku-4.5")
    print("  [3] claude-opus-4")
    choice = _prompt_choice(3)
    model_id = ["claude-sonnet-4", "claude-haiku-4.5", "claude-opus-4"][choice - 1]

    token_value = "$ANTHROPIC_API_KEY" if use_env_ref else api_key
    model_name = "anthropic-claude"
    models = {
        model_name: {
            "provider": "AnthropicProvider",
            "base_url": "https://api.anthropic.com",
            "auth_token": token_value,
            "model_id": model_id,
        }
    }

    print(f"\n  Model: {model_name} ({model_id})")

    return {"default_model": model_name, "models": models}


def _setup_openai() -> dict:
    """OpenAI 设置流程。"""
    api_key, use_env_ref = _prompt_api_key("OPENAI_API_KEY")

    print("\nSelect model:")
    print("  [1] gpt-4.1 (recommended)")
    print("  [2] gpt-4.1-mini")
    print("  [3] o3")
    choice = _prompt_choice(3)
    model_id = ["gpt-4.1", "gpt-4.1-mini", "o3"][choice - 1]

    token_value = "$OPENAI_API_KEY" if use_env_ref else api_key
    model_name = "openai-gpt"
    models = {
        model_name: {
            "provider": "OpenAIProvider",
            "base_url": "https://api.openai.com/v1",
            "auth_token": token_value,
            "model_id": model_id,
        }
    }

    print(f"\n  Model: {model_name} ({model_id})")

    return {"default_model": model_name, "models": models}


def _setup_other() -> dict:
    """自定义 OpenAI 兼容 API 设置流程。"""
    print()
    base_url = input("Base URL (e.g. https://api.example.com/v1): ").strip()
    api_key = input("API key: ").strip()
    model_id = input("Model ID (e.g. my-model): ").strip()

    if not base_url or not api_key or not model_id:
        print("Error: all fields are required.", file=sys.stderr)
        sys.exit(1)

    model_name = "custom"
    models = {
        model_name: {
            "provider": "OpenAIProvider",
            "base_url": base_url,
            "auth_token": api_key,
            "model_id": model_id,
        }
    }

    print(f"\n  Model: {model_name} ({model_id})")

    return {"default_model": model_name, "models": models}


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

    # 合并 models（已有的保留，新增的追加）
    existing_models = existing.get("models", {})
    new_models = new_data.get("models", {})
    existing_models.update(new_models)
    existing["models"] = existing_models

    # 设置 default_model（仅在没有时设置）
    if "default_model" not in existing and "default_model" in new_data:
        existing["default_model"] = new_data["default_model"]

    MUTBOT_CONFIG_PATH.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nConfig written to {MUTBOT_CONFIG_PATH}")
