"""mutbot.runtime.config -- mutbot 配置管理。

单层文件配置（~/.mutbot/config.json），内置持久化和变更通知。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from mutagent.config import (
    ChangeCallback,
    Config,
    ConfigChangeEvent,
    Disposable,
)

logger = logging.getLogger(__name__)

MUTBOT_USER_DIR = Path.home() / ".mutbot"


# ---------------------------------------------------------------------------
# 环境变量展开
# ---------------------------------------------------------------------------

def _expand_env(value: Any) -> Any:
    """递归展开配置值中的环境变量引用。"""
    if isinstance(value, str):
        return re.sub(
            r'\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
            lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# MutbotConfig
# ---------------------------------------------------------------------------

class MutbotConfig(Config):
    """mutbot 单层文件配置。

    管理 ~/.mutbot/config.json，内置持久化和变更通知。
    未来可演进为 LayeredConfig 支持多层合并。
    """

    _data: dict
    _listeners: list  # list[tuple[str, ChangeCallback]]
    _config_path: Path
    _last_write_mtime: float  # 防循环：最近一次自己写文件的 mtime

    def get(self, name: str, *, default: Any = None) -> Any:
        """点分路径导航 _data，递归展开环境变量。"""
        node = self._data
        for key in name.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return _expand_env(node)

    def set(self, name: str, value: Any, *, source: str = "") -> None:
        """按点分路径写入 _data，持久化到文件，触发匹配的 on_change 回调。"""
        # 写入 _data
        node = self._data
        keys = name.split(".")
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value
        # 持久化
        self._save()
        # 触发 on_change
        self._notify(name, source)

    def on_change(self, pattern: str, callback: ChangeCallback) -> Disposable:
        """注册监听。返回 Disposable 用于取消。"""
        entry = (pattern, callback)
        self._listeners.append(entry)

        def dispose() -> None:
            self._listeners.remove(entry)

        return Disposable(dispose=dispose)

    def reload(self) -> None:
        """从文件重新加载。逐个对比顶层 key，每个变化的 key 各触发一次 on_change。"""
        # 防循环：跳过自己写的文件
        try:
            current_mtime = self._config_path.stat().st_mtime
        except OSError:
            return
        if current_mtime == self._last_write_mtime:
            return

        # 读取新数据
        try:
            new_data = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        old_data = self._data
        self._data = new_data

        # 逐个对比顶层 key
        all_keys = set(old_data) | set(new_data)
        for key in all_keys:
            if old_data.get(key) != new_data.get(key):
                self._notify(key, "file_changed")

    def update_all(self, data: dict, *, source: str = "") -> None:
        """批量更新整个 _data，持久化，逐个对比顶层 key 触发 on_change。"""
        old_data = self._data
        self._data = data
        # 持久化
        self._save()
        # 逐个对比顶层 key
        all_keys = set(old_data) | set(data)
        for key in all_keys:
            if old_data.get(key) != data.get(key):
                self._notify(key, source)

    # --- 内部方法 ---

    def _save(self) -> None:
        """持久化 _data 到文件。"""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            self._last_write_mtime = self._config_path.stat().st_mtime
        except OSError:
            pass

    def _notify(self, key: str, source: str) -> None:
        """触发匹配的 on_change 回调。"""
        event = ConfigChangeEvent(key=key, source=source, config=self)
        for pattern, cb in list(self._listeners):
            if self.affects(pattern, key):
                cb(event)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def load_mutbot_config() -> MutbotConfig:
    """加载 mutbot 配置。"""
    config_path = MUTBOT_USER_DIR / "config.json"
    MUTBOT_USER_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load config from %s", config_path)
    mtime = 0.0
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        pass
    return MutbotConfig(
        _data=data,
        _listeners=[],
        _config_path=config_path,
        _last_write_mtime=mtime,
    )
