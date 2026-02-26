"""mutbot.runtime.config -- mutbot 配置加载。

两层配置合并（低 → 高优先级）：
1. ~/.mutbot/config.json    (mutbot 用户级，向导写入此处)
2. .mutbot/config.json      (项目级覆盖)
"""

from __future__ import annotations

from pathlib import Path

from mutagent.config import Config

MUTBOT_USER_DIR = Path.home() / ".mutbot"

MUTBOT_CONFIG_FILES = [
    "~/.mutbot/config.json",    # mutbot 用户级
    ".mutbot/config.json",      # 项目级（最高）
]


def load_mutbot_config() -> Config:
    """加载 mutbot 配置（两层合并）。"""
    return Config.load(MUTBOT_CONFIG_FILES)
