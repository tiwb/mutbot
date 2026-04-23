"""MutbotTools — mutbot 运行时调试能力注入 sandbox。

原 web/mcp.py 中的 8 个 MCPToolSet 子类合并到此处,作为单个
NamespaceTools 子类暴露,命名空间 ``mutbot.*``。

通过 ``pysandbox("mutbot.xxx(...)")`` 调用。函数清单见 ``help(mutbot)``。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import time
import traceback
import uuid
from typing import Any

import mutbot
from mutagent.sandbox.namespace import NamespaceTools

logger = logging.getLogger(__name__)

# 服务器启动时间（用于计算 uptime）
_start_time = time.monotonic()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_managers():
    """延迟获取全局 manager（避免 import 循环）。"""
    from mutbot.web import server as _srv
    return _srv


_SECRET_KEYWORDS = {"key", "token", "secret", "password", "credential"}


def _mask_secrets(obj: Any, _key: str = "") -> Any:
    """递归遍历 dict/list,将字段名含敏感关键词的字符串值替换为 '***'。"""
    if isinstance(obj, dict):
        return {k: _mask_secrets(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_secrets(v, _key) for v in obj]
    if isinstance(obj, str) and _key:
        key_lower = _key.lower()
        if any(kw in key_lower for kw in _SECRET_KEYWORDS):
            return "***"
    return obj


def _format_log_entries(entries: list[Any]) -> str:
    """日志条目列表 → 纯文本,一行一条。

    格式: [HH:MM:SS] LEVEL  logger — message
    多行 message（如 traceback）保持原样缩进输出。
    """
    if not entries:
        return "(no logs)"
    from datetime import datetime
    lines: list[str] = []
    for e in entries:
        ts = datetime.fromtimestamp(e.timestamp).strftime("%H:%M:%S.%f")[:12]
        level = e.level.ljust(7)
        header = f"[{ts}] {level} {e.logger_name} — "
        msg = e.message
        if "\n" in msg:
            first, *rest = msg.split("\n")
            indent = " " * len(header)
            msg = first + "\n" + "\n".join(indent + l for l in rest)
        lines.append(header + msg)
    return "\n".join(lines)


def _int(v: Any, default: int = 0) -> int:
    """安全转换为 int。"""
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


# ---------------------------------------------------------------------------
# exec_worker 内部:eval/exec Python 代码
# ---------------------------------------------------------------------------

class _AsyncResult(Exception):
    """内部异常:eval 结果是 coroutine,需要 await。"""
    def __init__(self, future: Any) -> None:
        self.future = future


def _safe_eval(code: str, namespace: dict[str, Any]) -> str:
    """eval/exec Python 代码,返回结果文本。

    先尝试 eval（表达式）,SyntaxError 时回退到 exec（语句）。
    eval 返回 repr(result);exec 捕获 stdout 输出。
    支持 coroutine 结果（自动 await）。
    """
    def _resolve(result: Any) -> str:
        if asyncio.iscoroutine(result):
            loop = asyncio.get_event_loop()
            future = asyncio.ensure_future(result, loop=loop)
            raise _AsyncResult(future)
        return repr(result)

    try:
        result = eval(code, namespace)
        return _resolve(result)
    except _AsyncResult:
        raise
    except SyntaxError:
        pass
    except Exception:
        return traceback.format_exc()

    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        exec(code, namespace)
        output = buf.getvalue()
        return output if output else "(no output)"
    except Exception:
        return buf.getvalue() + traceback.format_exc()
    finally:
        sys.stdout = old_stdout


# ---------------------------------------------------------------------------
# exec_frontend 内部:eval_id → Future 映射
# ---------------------------------------------------------------------------

# 被 mutbot.web.rpc_workspace.DebugRpc.eval_result 消费
_eval_js_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# config_set 白名单
# ---------------------------------------------------------------------------

_CONFIG_WHITELIST = {"logging.console_level", "default_model"}


# ---------------------------------------------------------------------------
# MutbotTools — NamespaceTools
# ---------------------------------------------------------------------------

class MutbotTools(NamespaceTools):
    """mutbot 运行时调试能力。

    sandbox 中调用:``mutbot.status()`` / ``mutbot.logs(...)`` 等。
    完整函数清单:``help(mutbot)``。
    """

    _namespace = "mutbot"

    # ----- 服务器状态 -----

    async def status(self) -> str:
        """获取服务器全局运行状态:uptime、session 数、workspace 数、连接数、内存占用。

        Returns:
            多行文本,包含版本、uptime、workspace/session/connection 数量、内存(MB)。
        """
        srv = _get_managers()
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

        mem_str = f"{mem_mb} MB" if mem_mb is not None else "N/A"
        return (
            f"mutbot v{mutbot.__version__}\n"
            f"Uptime:      {hours}h{minutes}m{seconds}s\n"
            f"Workspaces:  {ws_count}\n"
            f"Sessions:    {sess_count}\n"
            f"Connections: {client_count}\n"
            f"Memory:      {mem_str}"
        )

    async def restart(self) -> str:
        """触发服务器热重启。通过 Supervisor 的 /api/restart 端点平滑交接(drain 旧 Worker → spawn 新 Worker)。

        Returns:
            JSON 字符串,包含 Supervisor 返回内容,或 {"error": ...}。
        """
        import urllib.request
        srv = _get_managers()
        port = 8741
        if srv.config:
            listen = srv.config.get("listen", default=[])
            if listen:
                addr = listen[0] if isinstance(listen, list) and listen else str(listen)
                if ":" in str(addr):
                    port = int(str(addr).rsplit(":", 1)[1])
                elif str(addr).isdigit():
                    port = int(addr)

        url = f"http://127.0.0.1:{port}/api/restart"
        try:
            req = urllib.request.Request(url, data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # ----- workspace -----

    async def workspaces(self) -> str:
        """列出所有 workspace:id、名称、session 数量。

        Returns:
            每行一个 workspace:"<id8>  <name>  sessions=<n>"。
        """
        srv = _get_managers()
        if not srv.workspace_manager:
            return "(no workspaces)"
        lines: list[str] = []
        for ws in srv.workspace_manager.list_all():
            lines.append(
                f"{ws.id[:8]}  {ws.name or '(unnamed)'}  "
                f"sessions={len(ws.sessions)}"
            )
        return "\n".join(lines) if lines else "(no workspaces)"

    # ----- session -----

    async def sessions(self, workspace_id: str = "") -> str:
        """列出 session。

        Args:
            workspace_id: 可选,仅列出指定 workspace 的 session。

        Returns:
            每行一个 session:"<id8>  <type>  [<status>]  <model?>  <title?>"。
        """
        srv = _get_managers()
        if not srv.session_manager:
            return "(no sessions)"
        if workspace_id:
            sessions = srv.session_manager.list_by_workspace(workspace_id)
        else:
            sessions = list(srv.session_manager._sessions.values())
        if not sessions:
            return "(no sessions)"
        from mutbot.session import AgentSession
        lines: list[str] = []
        for s in sessions:
            parts = [s.id[:8], s.type or "?", f"[{s.status}]"]
            if isinstance(s, AgentSession) and s.model:
                parts.append(s.model)
            if s.title:
                parts.append(s.title)
            lines.append("  ".join(parts))
        return "\n".join(lines)

    async def session_inspect(self, session_id: str) -> str:
        """查看单个 session 的完整运行时状态:config、status、runtime 信息、通道数、token 用量。

        Args:
            session_id: session 的完整 ID(不支持前缀)。

        Returns:
            多行文本,含 Session/Type/Title/Status/Created/Updated/Model/Tokens/Runtime/Channels/Config。
        """
        srv = _get_managers()
        if not srv.session_manager:
            return "error: session_manager not initialized"
        s = srv.session_manager.get(session_id)
        if not s:
            return f"error: session {session_id} not found"

        from mutbot.session import AgentSession
        lines: list[str] = [
            f"Session:  {s.id}",
            f"Type:     {s.type}",
            f"Title:    {s.title or '(none)'}",
            f"Status:   {s.status}",
            f"Created:  {s.created_at}",
            f"Updated:  {s.updated_at}",
        ]

        if isinstance(s, AgentSession):
            lines.append(f"Model:    {s.model}")
            lines.append(f"Tokens:   {s.total_tokens}")

        rt = srv.session_manager.get_runtime(session_id)
        if rt:
            from mutbot.runtime.session_manager import AgentSessionRuntime
            lines.append(f"Runtime:  active")
            if isinstance(rt, AgentSessionRuntime):
                lines.append(f"  Agent:  {'yes' if rt.agent else 'no'}")
                lines.append(f"  Bridge: {'yes' if rt.bridge else 'no'}")
                if isinstance(s, AgentSession):
                    lines.append(f"  Context: {s.context_used}/{s.context_window}")
        else:
            lines.append(f"Runtime:  inactive")

        if srv.channel_manager:
            channels = srv.channel_manager.get_channels_for_session(session_id)
            lines.append(f"Channels: {len(channels)}")

        if s.config:
            lines.append(f"Config:   {json.dumps(s.config, ensure_ascii=False)}")

        return "\n".join(lines)

    async def session_messages(
        self, session_id: str, last_n: int = 10, role: str = "", full: bool = False,
    ) -> str:
        """查看 agent session 的对话历史。

        Args:
            session_id: session 的完整 ID。
            last_n: 返回最近 N 条消息,默认 10。
            role: 可选过滤 "user"/"assistant"/"tool"。
            full: True 返回完整内容,False 截取前 500 字符(默认)。

        Returns:
            多行文本,"--- [role] ---" 分隔每条消息,含 tool 调用名和 token 信息。
        """
        last_n = _int(last_n, 10)
        full = _bool(full)
        srv = _get_managers()
        if not srv.session_manager:
            return "error: session_manager not initialized"

        from mutbot.runtime.session_manager import AgentSessionRuntime
        rt = srv.session_manager.get_runtime(session_id)
        if not rt or not isinstance(rt, AgentSessionRuntime) or not rt.agent:
            return f"error: no agent runtime for session {session_id}"

        messages = rt.agent.context.messages
        if role:
            messages = [m for m in messages if m.role == role]
        messages = messages[-last_n:]

        lines: list[str] = []
        for m in messages:
            text_parts: list[str] = []
            tool_names: list[str] = []
            for b in m.blocks:
                if b.type == "text":
                    text_parts.append(b.text)
                elif b.type == "tool_use":
                    tool_names.append(f"{b.name}({b.status})")
                elif b.type == "thinking":
                    if b.thinking:
                        text_parts.append(
                            f"[thinking: {b.thinking[:100]}...]"
                            if len(b.thinking) > 100 else f"[thinking: {b.thinking}]"
                        )

            content = "\n".join(text_parts)
            if not full and len(content) > 500:
                content = content[:500] + "..."

            token_info = ""
            if m.input_tokens or m.output_tokens:
                token_info = f"  (in={m.input_tokens} out={m.output_tokens})"
            header = f"--- [{m.role}]{token_info} ---"
            lines.append(header)
            if tool_names:
                lines.append(f"tools: {', '.join(tool_names)}")
            if content:
                lines.append(content)

        return "\n".join(lines) if lines else "(no messages)"

    # ----- connections -----

    async def connections(self) -> str:
        """列出所有活跃的 WebSocket 客户端连接。

        Returns:
            每行一个连接:"<id8>  <state>  ws=<ws_id8>  ch=<n>  buf=<bytes>  sent=<n>  recv=<n>"。
        """
        from mutbot.web.routes import _clients
        srv = _get_managers()

        if not _clients:
            return "(no connections)"
        lines: list[str] = []
        for cid, client in _clients.items():
            channels = []
            if srv.channel_manager:
                channels = srv.channel_manager.get_channels_for_client(cid)
            buf = client._send_buffer._current_bytes
            buf_str = f"{buf}B" if buf < 1024 else f"{buf/1024:.1f}KB"
            lines.append(
                f"{cid[:8]}  {client.state}  ws={client.workspace_id[:8] if client.workspace_id else 'N/A'}  "
                f"ch={len(channels)}  buf={buf_str}  "
                f"sent={client._send_buffer._total_sent}  recv={client._recv_count}"
            )
        return "\n".join(lines)

    # ----- logs -----

    async def logs(
        self, level: str = "INFO", logger: str = "",
        pattern: str = "", last_n: int = 50,
    ) -> str:
        """查询内存中的日志。

        Args:
            level: 最低级别 DEBUG/INFO/WARNING/ERROR,默认 INFO。
            logger: logger 名前缀匹配。
            pattern: 正则匹配消息内容。
            last_n: 返回条数,默认 50。

        Returns:
            格式化日志文本,每行一条 "[HH:MM:SS] LEVEL  logger — message"。
        """
        last_n = _int(last_n, 50)
        srv = _get_managers()
        if not srv.log_store:
            return json.dumps({"error": "log_store not initialized"})

        entries = srv.log_store.query(
            pattern=pattern, level=level, limit=last_n, logger_name=logger,
        )
        return _format_log_entries(entries)

    async def errors(self, last_n: int = 20) -> str:
        """获取最近的 ERROR 和 WARNING 级别日志,用于快速排查问题。

        Args:
            last_n: 返回条数,默认 20。

        Returns:
            格式化日志文本,等价 logs(level="WARNING", last_n=N)。
        """
        last_n = _int(last_n, 20)
        srv = _get_managers()
        if not srv.log_store:
            return json.dumps({"error": "log_store not initialized"})

        entries = srv.log_store.query(level="WARNING", limit=last_n)
        return _format_log_entries(entries)

    # ----- config -----

    async def config_get(self, key: str = "") -> str:
        """读取运行时配置。敏感字段(含 key/token/secret/password/credential)自动脱敏为 ***。

        Args:
            key: 点号路径(如 "default_model"),空字符串返回完整配置。

        Returns:
            JSON 字符串。key 指定时为 {"key": ..., "value": ...};否则完整配置 dict。
        """
        srv = _get_managers()
        if not srv.config:
            return json.dumps({"error": "config not initialized"})
        if key:
            value = srv.config.get(key, default=None)
            return json.dumps({"key": key, "value": value}, ensure_ascii=False)
        data = _mask_secrets(srv.config._data)
        return json.dumps(data, ensure_ascii=False, indent=2)

    async def config_set(self, key: str, value: str) -> str:
        """热更新配置项(触发 on_change 回调)。仅允许修改白名单内的配置项。

        Args:
            key: 白名单内的 key。当前白名单:"logging.console_level"、"default_model"。
            value: 新值(字符串)。

        Returns:
            JSON 字符串 {"ok": true, ...},或 {"error": ..., "allowed": [...]}。
        """
        srv = _get_managers()
        if not srv.config:
            return json.dumps({"error": "config not initialized"})
        if key not in _CONFIG_WHITELIST:
            return json.dumps({
                "error": f"key '{key}' not in whitelist",
                "allowed": sorted(_CONFIG_WHITELIST),
            })
        srv.config.set(key, value, source="sandbox")
        return json.dumps({"ok": True, "key": key, "value": value})

    # ----- exec_* —— Python 进程执行 -----

    async def exec_worker(self, code: str) -> str:
        """在 worker 进程(当前服务主进程)中执行 Python 代码。

        真实命名空间包含 srv/tm/sm/wm/cm/config 等 live manager。
        eval 返回表达式值,exec 捕获 print 输出。错误返回 traceback。
        支持 async 表达式(自动 await)。

        Args:
            code: Python 源代码(表达式或语句)。

        Returns:
            执行结果文本或 traceback。
        """
        srv = _get_managers()
        namespace: dict[str, Any] = {
            "__builtins__": __builtins__,
            "srv": srv,
            "tm": getattr(srv, "terminal_manager", None),
            "sm": getattr(srv, "session_manager", None),
            "wm": getattr(srv, "workspace_manager", None),
            "cm": getattr(srv, "channel_manager", None),
            "config": getattr(srv, "config", None),
        }
        try:
            return _safe_eval(code, namespace)
        except _AsyncResult as ar:
            try:
                result = await ar.future
                return repr(result)
            except Exception:
                return traceback.format_exc()

    async def exec_ptyhost(self, code: str) -> str:
        """在 ptyhost 进程中执行 Python 代码。

        Args:
            code: Python 源代码。

        Returns:
            ptyhost 返回的 result 文本,或错误说明。
        """
        srv = _get_managers()
        tm = getattr(srv, "terminal_manager", None)
        if not tm or not tm._client or not tm._client.connected:
            return "error: ptyhost not connected"
        try:
            reply = await tm._client.eval_code(code)
            return reply.get("result", repr(reply))
        except Exception:
            return traceback.format_exc()

    async def exec_supervisor(self, code: str) -> str:
        """在 supervisor 进程中执行 Python 代码(通过 /api/eval HTTP 端点)。

        Args:
            code: Python 源代码。

        Returns:
            Supervisor 返回的响应文本,或 traceback。
        """
        import urllib.request
        srv = _get_managers()
        port = 8741
        if srv.config:
            listen = srv.config.get("listen", default=[])
            if listen:
                addr = listen[0] if isinstance(listen, list) and listen else str(listen)
                if ":" in str(addr):
                    port = int(str(addr).rsplit(":", 1)[1])
                elif str(addr).isdigit():
                    port = int(addr)

        url = f"http://127.0.0.1:{port}/api/eval"
        body = json.dumps({"code": code}).encode("utf-8")
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception:
            return traceback.format_exc()

    # ----- exec_frontend —— 浏览器 JS 执行 -----

    async def exec_frontend(self, code: str, client_id: str = "") -> str:
        """在 mutbot 前端(浏览器)执行 JavaScript 代码。

        通过 WebSocket 推送到目标客户端执行,10 秒超时。

        Args:
            code: JavaScript 源代码。
            client_id: 可选,按前缀匹配指定客户端;默认第一个 connected 客户端。

        Returns:
            执行结果文本,或 JSON {"error": ...}。
        """
        from mutbot.web.routes import _clients

        client = None
        if client_id:
            for cid, c in _clients.items():
                if cid.startswith(client_id) and c.state == "connected":
                    client = c
                    break
            if not client:
                return json.dumps({"error": f"client not found: {client_id}"})
        else:
            for c in _clients.values():
                if c.state == "connected":
                    client = c
                    break
            if not client:
                return json.dumps({"error": "no connected client"})

        eval_id = uuid.uuid4().hex[:8]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        _eval_js_pending[eval_id] = future

        client.enqueue("json", {
            "type": "event",
            "event": "eval_js",
            "data": {"id": eval_id, "code": code},
        })

        try:
            result = await asyncio.wait_for(future, timeout=10)
            if result.get("error"):
                return json.dumps({"error": result["error"]})
            return result.get("result", "(undefined)")
        except asyncio.TimeoutError:
            return json.dumps({"error": "timeout"})
        finally:
            _eval_js_pending.pop(eval_id, None)
