"""Claude Code CLI 子进程管理 + ClaudeCodeSession @impl。

spawn Claude Code CLI 子进程，通过 stream-json stdin/stdout 管道通信。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from typing import Any, Callable, TYPE_CHECKING

from mutobj import impl

if TYPE_CHECKING:
    from mutbot.channel import Channel, ChannelContext
    from mutbot.runtime.session_manager import SessionManager

logger = logging.getLogger(__name__)

# stdout 消息回调类型
MessageCallback = Callable[[dict[str, Any]], None]
# 进程退出回调
ExitCallback = Callable[[int | None], None]


class ClaudeCodeProcess:
    """管理一个 Claude Code CLI 子进程（stream-json 模式）。"""

    def __init__(
        self,
        *,
        cwd: str = ".",
        model: str = "",
        permission_mode: str = "",
        resume_session_id: str = "",
        on_message: MessageCallback | None = None,
        on_exit: ExitCallback | None = None,
    ) -> None:
        self._cwd = cwd
        self._model = model
        self._permission_mode = permission_mode
        self._resume_session_id = resume_session_id
        self.on_message = on_message
        self.on_exit = on_exit

        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._started = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """spawn CLI 子进程。"""
        if self._started:
            return

        claude_path = _find_claude_cli()
        args = [
            claude_path,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--permission-prompt-tool", "stdio",
        ]
        if self._model:
            args.extend(["--model", self._model])
        if self._permission_mode:
            args.extend(["--permission-mode", self._permission_mode])
        if self._resume_session_id:
            args.extend(["--resume", self._resume_session_id])

        env = _build_env()

        logger.info("Spawning Claude Code CLI: %s (cwd=%s)", " ".join(args), self._cwd)

        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=env,
        )
        self._started = True
        self._read_task = asyncio.create_task(self._read_loop(), name="claude-code-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="claude-code-stderr")

    async def _read_loop(self) -> None:
        """逐行读取 stdout（每行一个 JSON），回调分发。"""
        assert self._process and self._process.stdout
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON stdout line: %s", line_str[:200])
                    continue
                if self.on_message:
                    try:
                        self.on_message(msg)
                    except Exception:
                        logger.exception("on_message callback error")
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("stdout read loop error")
        finally:
            # 等待进程退出
            if self._process:
                exit_code = await self._process.wait()
                logger.info("Claude Code CLI exited with code %s", exit_code)
                if self.on_exit:
                    try:
                        self.on_exit(exit_code)
                    except Exception:
                        logger.exception("on_exit callback error")

    async def _read_stderr(self) -> None:
        """读取 stderr，记录到日志。"""
        assert self._process and self._process.stderr
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("[claude-code stderr] %s", text)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("stderr read loop error")

    def write(self, msg: dict[str, Any]) -> None:
        """JSON 序列化 + 换行写入 stdin。"""
        if not self._process or not self._process.stdin:
            logger.warning("Cannot write: process not running")
            return
        data = json.dumps(msg, ensure_ascii=False) + "\n"
        self._process.stdin.write(data.encode("utf-8"))

    async def stop(self, timeout: float = 5.0) -> None:
        """优雅关闭：先关 stdin，等进程退出，超时后 kill。"""
        if not self._process:
            return

        # 关闭 stdin 让 CLI 自行退出
        if self._process.stdin and not self._process.stdin.is_closing():
            self._process.stdin.close()

        try:
            await asyncio.wait_for(self._process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Claude Code CLI did not exit in %ss, killing", timeout)
            self._process.kill()
            await self._process.wait()

        # 取消读取任务
        for task in (self._read_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._process = None
        self._started = False


def _find_claude_cli() -> str:
    """查找 claude CLI 可执行文件路径。"""
    # 优先用 PATH 上的 claude
    found = shutil.which("claude")
    if found:
        return found
    # Windows 常见路径
    if sys.platform == "win32":
        npm_global = os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")
        if os.path.isfile(npm_global):
            return npm_global
    raise FileNotFoundError(
        "Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
    )


def _build_env() -> dict[str, str]:
    """构建子进程环境变量。"""
    env = os.environ.copy()
    # 避免嵌套检测
    env.pop("CLAUDECODE", None)
    # Windows 上需要 Git Bash 路径
    if sys.platform == "win32" and "CLAUDE_CODE_GIT_BASH_PATH" not in env:
        git_bash = shutil.which("bash")
        if git_bash:
            env["CLAUDE_CODE_GIT_BASH_PATH"] = git_bash
    return env


# ---------------------------------------------------------------------------
# ClaudeCodeSession @impl — 生命周期
# ---------------------------------------------------------------------------

from mutbot.session import ClaudeCodeSession


@impl(ClaudeCodeSession.on_create)
async def _claude_code_on_create(self: ClaudeCodeSession, sm: SessionManager) -> None:
    """初始化配置，创建 ClaudeCodeProcess 实例（不启动）。"""
    self.cwd = self.config.get("cwd", ".")
    self.model = self.config.get("model", "")
    self.permission_mode = self.config.get("permission_mode", "")
    self.status = "created"


@impl(ClaudeCodeSession.on_stop)
def _claude_code_on_stop(self: ClaudeCodeSession, sm: SessionManager) -> None:
    """标记停止。进程清理由 _ClaudeCodeRuntime 负责。"""
    runtime = _runtimes.pop(self.id, None)
    if runtime:
        loop = asyncio.get_event_loop()
        loop.create_task(runtime.stop())
    self.status = "stopped"


@impl(ClaudeCodeSession.on_restart_cleanup)
def _claude_code_on_restart_cleanup(self: ClaudeCodeSession) -> None:
    """服务器重启时标记 stopped（CLI 子进程不可恢复）。"""
    self.status = "stopped"


# ---------------------------------------------------------------------------
# ClaudeCodeSession @impl — Channel 通信
# ---------------------------------------------------------------------------

# session.id → ClaudeCodeProcess 映射
_runtimes: dict[str, ClaudeCodeProcess] = {}


@impl(ClaudeCodeSession.on_connect)
async def _claude_code_on_connect(
    self: ClaudeCodeSession, channel: Channel, ctx: ChannelContext,
) -> None:
    """首个 channel 连接时 spawn CLI 子进程。"""
    key = self.id

    # 已有运行中的进程 → 回放消息历史
    runtime = _runtimes.get(key)
    if runtime and runtime.running:
        history = _message_history.get(key, [])
        for msg in history:
            channel.send_json({"type": "claude_code_event", "event": msg})
        channel.send_json({"type": "ready", "alive": True})
        return

    # 首次连接：创建并启动进程
    history_list: list[dict[str, Any]] = []
    _message_history[key] = history_list

    def on_message(msg: dict[str, Any]) -> None:
        # 缓存消息用于重连回放
        history_list.append(msg)
        # 提取 session_id
        if msg.get("type") == "system" and msg.get("subtype") == "init":
            sid = msg.get("session_id", "")
            if sid:
                self.claude_session_id = sid
            self.status = "running"
        # 广播给所有前端 channel
        self.broadcast_json({"type": "claude_code_event", "event": msg})

    def on_exit(exit_code: int | None) -> None:
        self.status = "stopped"
        self.broadcast_json({
            "type": "claude_code_event",
            "event": {"type": "process_exited", "exit_code": exit_code},
        })

    proc = ClaudeCodeProcess(
        cwd=self.cwd,
        model=self.model,
        permission_mode=self.permission_mode,
        resume_session_id=self.claude_session_id,
        on_message=on_message,
        on_exit=on_exit,
    )
    _runtimes[key] = proc

    await proc.start()
    channel.send_json({"type": "ready", "alive": True})


@impl(ClaudeCodeSession.on_disconnect)
def _claude_code_on_disconnect(
    self: ClaudeCodeSession, channel: Channel, ctx: ChannelContext,
) -> None:
    """断开时不杀进程（支持重连）。"""
    pass


@impl(ClaudeCodeSession.on_message)
async def _claude_code_on_message(
    self: ClaudeCodeSession, channel: Channel, raw: dict, ctx: ChannelContext,
) -> None:
    """处理前端消息，转发到 CLI stdin。"""
    runtime = _runtimes.get(self.id)
    if not runtime or not runtime.running:
        channel.send_json({"type": "error", "message": "CLI process not running"})
        return

    msg_type = raw.get("type", "")

    if msg_type == "user_message":
        # 用户发消息 → SDKUserMessage 格式写入 stdin
        text = raw.get("text", "")
        sdk_msg = {
            "type": "user",
            "session_id": self.claude_session_id or "",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
            "parent_tool_use_id": None,
        }
        runtime.write(sdk_msg)

    elif msg_type == "permission_response":
        # 权限回复
        request_id = raw.get("request_id", "")
        behavior = raw.get("behavior", "deny")
        runtime.write({
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {"behavior": behavior},
            },
        })

    elif msg_type == "interrupt":
        # 中断
        runtime.write({
            "type": "control_request",
            "request": {"subtype": "interrupt"},
        })

    elif msg_type == "control":
        # 其他控制指令（set_model, set_permission_mode 等）
        subtype = raw.get("subtype", "")
        payload = raw.get("payload", {})
        runtime.write({
            "type": "control_request",
            "request": {"subtype": subtype, **payload},
        })

    else:
        logger.warning("Unknown message type from frontend: %s", msg_type)


# 消息历史缓存（用于重连回放）
_message_history: dict[str, list[dict[str, Any]]] = {}
