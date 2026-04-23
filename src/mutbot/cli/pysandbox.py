"""`mutbot pysandbox` — 向活 server 的 MCP endpoint 提交 Python 代码执行。

对齐 python CLI 约定:
  mutbot pysandbox -c "code"      # 单条代码
  mutbot pysandbox script.py       # 脚本文件
  mutbot pysandbox -                # 从 stdin 读
  echo "code" | mutbot pysandbox    # 管道

server 未运行时直接报错退出;不提供脱机执行。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Sequence

from mutagent.net.client import MCPClient, MCPError


DEFAULT_PORT = 8741
DEFAULT_TIMEOUT = 30.0


def _read_code(args: argparse.Namespace) -> str:
    if args.code is not None:
        return args.code
    if args.script == "-":
        return sys.stdin.read()
    if args.script is not None:
        with open(args.script, "r", encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print(
        "Error: no code provided. Use -c CODE, a script file, or pipe via stdin.",
        file=sys.stderr,
    )
    print("Examples:", file=sys.stderr)
    print('  mutbot pysandbox -c "mutbot.status()"', file=sys.stderr)
    print("  mutbot pysandbox script.py", file=sys.stderr)
    print('  echo "mutbot.status()" | mutbot pysandbox', file=sys.stderr)
    sys.exit(2)


def _format_result(result: dict[str, Any]) -> tuple[str, bool]:
    """把 MCPClient.call_tool 的返回拍成文本。返回 (text, is_error)。"""
    is_error = bool(result.get("isError"))
    content = result.get("content") or []
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    if parts:
        return "\n".join(parts), is_error
    # 没有 text 项,退回 JSON
    import json
    return json.dumps(result, ensure_ascii=False, indent=2), is_error


def _server_unreachable_message(port: int, reason: str) -> str:
    return (
        f"Error: mutbot server not running at http://127.0.0.1:{port} ({reason})\n"
        f"\n"
        f"To start:           python -m mutbot\n"
        f"Read logs offline:  tail -100 ~/.mutbot/logs/server-*.log\n"
    )


async def _run(code: str, port: int, timeout: float) -> int:
    url = f"http://127.0.0.1:{port}/mcp"
    client = MCPClient(url=url, client_name="mutbot-pysandbox-cli", timeout=timeout)
    try:
        await client.connect()  # type: ignore[attr-defined]
    except (ConnectionError, OSError, TimeoutError) as e:
        print(_server_unreachable_message(port, str(e)), file=sys.stderr)
        return 1
    except Exception as e:
        # connect 内部可能是其他异常类型(例如 httpx / 异常包装)
        print(_server_unreachable_message(port, str(e)), file=sys.stderr)
        return 1

    try:
        try:
            result = await client.call_tool("pysandbox", code=code)  # type: ignore[attr-defined]
        except MCPError as e:
            print(f"Error: MCP {e.code}: {e.message}", file=sys.stderr)
            return 1
    finally:
        try:
            await client.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    text, is_error = _format_result(result)
    stream = sys.stderr if is_error else sys.stdout
    if text:
        print(text, file=stream)
    return 1 if is_error else 0


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mutbot pysandbox",
        description="Run Python code in the running mutbot server's sandbox.",
    )
    parser.add_argument(
        "-c",
        dest="code",
        metavar="CODE",
        help="code string (like python -c)",
    )
    parser.add_argument(
        "script",
        nargs="?",
        help="script file path, or '-' to read from stdin",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"mutbot server port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"RPC timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    args = parser.parse_args(argv)

    if args.code is not None and args.script is not None:
        parser.error("-c CODE and script file are mutually exclusive")

    code = _read_code(args)
    if not code.strip():
        print("Error: empty code.", file=sys.stderr)
        sys.exit(2)

    exit_code = asyncio.run(_run(code, args.port, args.timeout))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
