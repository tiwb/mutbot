"""mutbot.auth.setup_token — 一次性 setup token（公网无 auth 时使用）。

启动时生成 UUID4 token，验证通过后失效。
用于确认远程访问者能看到服务器控制台输出 = 拥有本地系统权限。

Supervisor 模式下，token 通过环境变量 MUTBOT_SETUP_TOKEN 传递给 Worker 子进程，
确保跨进程共享。
"""

from __future__ import annotations

import hmac
import os
import uuid

_ENV_KEY = "MUTBOT_SETUP_TOKEN"

# 模块级存储（Worker 子进程通过环境变量继承）
_token: str | None = os.environ.get(_ENV_KEY)


def generate() -> str:
    """生成新的 setup token（覆盖旧的）。返回 token 字符串。

    同时写入环境变量，确保后续 spawn 的子进程自动继承。
    """
    global _token
    _token = str(uuid.uuid4())
    os.environ[_ENV_KEY] = _token
    return _token


def verify(token: str) -> bool:
    """验证 token 是否正确（常量时间比较，防止时序攻击）。"""
    if not _token or not token:
        return False
    return hmac.compare_digest(token, _token)


def invalidate() -> None:
    """使 token 失效（auth 配置完成后调用）。"""
    global _token
    _token = None
    os.environ.pop(_ENV_KEY, None)


def is_active() -> bool:
    """是否有活跃的 setup token。"""
    return _token is not None
