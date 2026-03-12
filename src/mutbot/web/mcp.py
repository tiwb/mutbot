"""MCP 运行时内省 — MCPView + MCPToolSet 子类。

让 Claude Code 等 AI 工具通过 MCP 协议内省 mutbot 运行时状态。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import mutbot
from mutagent.net.mcp import MCPView, MCPToolSet

logger = logging.getLogger(__name__)

# 服务器启动时间（用于计算 uptime）
_start_time = time.monotonic()


class MutBotMCP(MCPView):
    path = "/mcp"
    name = "mutbot"
    version = mutbot.__version__
    instructions = "MutBot 运行时内省工具。查看服务器状态、session、workspace、连接、日志、配置。"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_managers():
    """延迟获取全局 manager（避免 import 循环）。"""
    from mutbot.web import server as _srv
    return _srv


_SECRET_KEYWORDS = {"key", "token", "secret", "password", "credential"}


def _mask_secrets(obj: Any, _key: str = "") -> Any:
    """递归遍历 dict/list，将字段名含敏感关键词的字符串值替换为 '***'。"""
    if isinstance(obj, dict):
        return {k: _mask_secrets(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_secrets(v, _key) for v in obj]
    if isinstance(obj, str) and _key:
        key_lower = _key.lower()
        if any(kw in key_lower for kw in _SECRET_KEYWORDS):
            return "***"
    return obj


def _int(v: Any, default: int = 0) -> int:
    """安全转换为 int（MCP schema 可能将 int 参数传为 string）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bool(v: Any) -> bool:
    """安全转换为 bool。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    return bool(v)


def _session_summary(s) -> dict[str, Any]:
    """Session → 摘要 dict。"""
    from mutbot.session import AgentSession
    d: dict[str, Any] = {
        "id": s.id,
        "workspace_id": s.workspace_id,
        "title": s.title,
        "type": s.type,
        "status": s.status,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }
    if isinstance(s, AgentSession):
        d["model"] = s.model
        d["total_tokens"] = s.total_tokens
    return d


# ---------------------------------------------------------------------------
# Server Tools
# ---------------------------------------------------------------------------

class ServerTools(MCPToolSet):
    """服务器级别的内省工具。"""
    path = "/mcp"

    async def server_status(self) -> str:
        """获取服务器全局运行状态：uptime、session 数、workspace 数、连接数、内存占用。"""
        srv = _get_managers()
        # 获取进程内存（跨平台，无额外依赖）
        try:
            import psutil
            mem_mb = round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
        except ImportError:
            mem_mb = None

        ws_count = len(srv.workspace_manager._workspaces) if srv.workspace_manager else 0
        sess_count = len(srv.session_manager._sessions) if srv.session_manager else 0

        from mutbot.web.routes import _clients
        client_count = len(_clients)

        uptime = time.monotonic() - _start_time
        hours, remainder = divmod(int(uptime), 3600)
        minutes, seconds = divmod(remainder, 60)

        return json.dumps({
            "version": mutbot.__version__,
            "uptime": f"{hours}h{minutes}m{seconds}s",
            "uptime_seconds": round(uptime),
            "workspaces": ws_count,
            "sessions": sess_count,
            "connections": client_count,
            "memory_mb": mem_mb,
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Workspace Tools
# ---------------------------------------------------------------------------

class WorkspaceTools(MCPToolSet):
    """Workspace 内省工具。"""
    path = "/mcp"

    async def list_workspaces(self) -> str:
        """列出所有 workspace：id、名称、路径、session 数量、最近访问时间。"""
        srv = _get_managers()
        if not srv.workspace_manager:
            return "[]"
        result = []
        for ws in srv.workspace_manager.list_all():
            result.append({
                "id": ws.id,
                "name": ws.name,
                "project_path": ws.project_path,
                "session_count": len(ws.sessions),
                "last_accessed_at": ws.last_accessed_at,
            })
        return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Session Tools
# ---------------------------------------------------------------------------

class SessionTools(MCPToolSet):
    """Session 内省工具。"""
    path = "/mcp"

    async def list_sessions(self, workspace_id: str = "") -> str:
        """列出 session 列表。可选按 workspace_id 过滤。返回 id、类型、标题、状态、模型等。"""
        srv = _get_managers()
        if not srv.session_manager:
            return "[]"
        if workspace_id:
            sessions = srv.session_manager.list_by_workspace(workspace_id)
        else:
            sessions = list(srv.session_manager._sessions.values())
        return json.dumps([_session_summary(s) for s in sessions], ensure_ascii=False)

    async def inspect_session(self, session_id: str) -> str:
        """查看单个 session 的完整运行时状态：config、status、runtime 信息、通道数、token 用量。"""
        srv = _get_managers()
        if not srv.session_manager:
            return json.dumps({"error": "session_manager not initialized"})
        s = srv.session_manager.get(session_id)
        if not s:
            return json.dumps({"error": f"session {session_id} not found"})

        from mutbot.session import AgentSession
        d = _session_summary(s)
        d["config"] = s.config

        # Runtime 信息
        rt = srv.session_manager.get_runtime(session_id)
        if rt:
            from mutbot.runtime.session_manager import AgentSessionRuntime
            d["has_runtime"] = True
            if isinstance(rt, AgentSessionRuntime):
                d["has_agent"] = rt.agent is not None
                d["has_bridge"] = rt.bridge is not None
                if isinstance(s, AgentSession):
                    d["context_used"] = s.context_used
                    d["context_window"] = s.context_window
        else:
            d["has_runtime"] = False

        # 通道数
        if srv.channel_manager:
            channels = srv.channel_manager.get_channels_for_session(session_id)
            d["channel_count"] = len(channels)

        return json.dumps(d, ensure_ascii=False)

    async def get_session_messages(
        self, session_id: str, last_n: int = 10, role: str = "", full: bool = False,
    ) -> str:
        """查看 agent session 的对话历史。默认最近 10 条，截取前 500 字符。role 可过滤 user/assistant/tool。full=true 返回完整内容。"""
        last_n = _int(last_n, 10)
        full = _bool(full)
        srv = _get_managers()
        if not srv.session_manager:
            return json.dumps({"error": "session_manager not initialized"})

        from mutbot.runtime.session_manager import AgentSessionRuntime
        rt = srv.session_manager.get_runtime(session_id)
        if not rt or not isinstance(rt, AgentSessionRuntime) or not rt.agent:
            return json.dumps({"error": f"no agent runtime for session {session_id}"})

        messages = rt.agent.context.messages
        if role:
            messages = [m for m in messages if m.role == role]
        messages = messages[-last_n:]

        result = []
        for m in messages:
            # 提取文本内容
            text_parts = []
            tool_calls = []
            for b in m.blocks:
                if b.type == "text":
                    text_parts.append(b.text)
                elif b.type == "tool_use":
                    tool_calls.append({"name": b.name, "status": b.status})
                elif b.type == "thinking":
                    if b.thinking:
                        text_parts.append(f"[thinking: {b.thinking[:100]}...]" if len(b.thinking) > 100 else f"[thinking: {b.thinking}]")

            content = "\n".join(text_parts)
            if not full and len(content) > 500:
                content = content[:500] + "..."

            entry: dict[str, Any] = {
                "role": m.role,
                "content": content,
                "input_tokens": m.input_tokens,
                "output_tokens": m.output_tokens,
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            result.append(entry)

        return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Connection Tools
# ---------------------------------------------------------------------------

class ConnectionTools(MCPToolSet):
    """客户端连接内省工具。"""
    path = "/mcp"

    async def list_connections(self) -> str:
        """列出所有活跃的 WebSocket 客户端连接：id、workspace、状态、通道数、缓冲区大小。"""
        from mutbot.web.routes import _clients
        srv = _get_managers()

        result = []
        for cid, client in _clients.items():
            channels = []
            if srv.channel_manager:
                channels = srv.channel_manager.get_channels_for_client(cid)
            result.append({
                "client_id": cid,
                "workspace_id": client.workspace_id,
                "state": client.state,
                "channel_count": len(channels),
                "buffer_bytes": client._send_buffer._current_bytes,
                "total_sent": client._send_buffer._total_sent,
                "total_received": client._recv_count,
            })
        return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Log Tools
# ---------------------------------------------------------------------------

class LogTools(MCPToolSet):
    """日志查询工具。"""
    path = "/mcp"

    async def query_logs(
        self, level: str = "INFO", logger: str = "", pattern: str = "", last_n: int = 50,
    ) -> str:
        """查询内存中的日志。level: DEBUG/INFO/WARNING/ERROR。logger: logger 名前缀匹配。pattern: 正则匹配消息。last_n: 返回条数（默认 50）。"""
        last_n = _int(last_n, 50)
        srv = _get_managers()
        if not srv.log_store:
            return json.dumps({"error": "log_store not initialized"})

        entries = srv.log_store.query(
            pattern=pattern, level=level, limit=last_n, logger_name=logger,
        )
        result = []
        for e in entries:
            result.append({
                "timestamp": e.timestamp,
                "level": e.level,
                "logger": e.logger_name,
                "message": e.message,
            })
        return json.dumps(result, ensure_ascii=False)

    async def get_errors(self, last_n: int = 20) -> str:
        """获取最近的 ERROR 和 WARNING 级别日志，用于快速排查问题。"""
        last_n = _int(last_n, 20)
        srv = _get_managers()
        if not srv.log_store:
            return json.dumps({"error": "log_store not initialized"})

        entries = srv.log_store.query(level="WARNING", limit=last_n)
        result = []
        for e in entries:
            result.append({
                "timestamp": e.timestamp,
                "level": e.level,
                "logger": e.logger_name,
                "message": e.message,
            })
        return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Config Tools
# ---------------------------------------------------------------------------

# set_config 白名单
_CONFIG_WHITELIST = {"logging.console_level", "default_model"}


class ConfigTools(MCPToolSet):
    """配置读取和热更新工具。"""
    path = "/mcp"

    async def get_config(self, key: str = "") -> str:
        """读取运行时配置。key 为空返回完整配置，指定 key 返回对应值（如 'default_model'）。"""
        srv = _get_managers()
        if not srv.config:
            return json.dumps({"error": "config not initialized"})
        if key:
            value = srv.config.get(key, default=None)
            return json.dumps({"key": key, "value": value}, ensure_ascii=False)
        # 完整配置（隐藏敏感字段）
        data = _mask_secrets(srv.config._data)
        return json.dumps(data, ensure_ascii=False, indent=2)

    async def set_config(self, key: str, value: str) -> str:
        """热更新配置项（触发 on_change 回调）。仅允许修改白名单内的配置项。"""
        srv = _get_managers()
        if not srv.config:
            return json.dumps({"error": "config not initialized"})
        if key not in _CONFIG_WHITELIST:
            return json.dumps({
                "error": f"key '{key}' not in whitelist",
                "allowed": sorted(_CONFIG_WHITELIST),
            })
        srv.config.set(key, value, source="mcp")
        return json.dumps({"ok": True, "key": key, "value": value})
