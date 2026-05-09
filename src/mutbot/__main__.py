"""MutBot entry point: mutbot [command]

子命令：
  serve (默认)  启动服务器
  worker         Worker 进程（Supervisor 内部使用）
  restart       触发热重启
  pysandbox     向运行中 server 的沙箱执行 Python 代码

示例：
  mutbot                                  # 默认 serve，监听 127.0.0.1:8741
  mutbot serve --listen :8888 --debug     # serve 显式调用
  mutbot restart --port 8888              # 热重启
  mutbot pysandbox -c "mutbot.status()"   # 沙箱执行
"""

import argparse
import sys


def _restart_command(port: int) -> None:
    """发送 POST /api/restart 到运行中的 Supervisor，等待新 Worker 就绪。"""
    import json
    import time
    import urllib.request

    base_url = f"http://127.0.0.1:{port}"

    # 1. 获取当前 Worker 信息
    try:
        req = urllib.request.Request(f"{base_url}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            health = json.loads(resp.read())
            old_gen = health.get("worker", {}).get("generation", 0)
    except Exception as e:
        print(f"Error: Cannot connect to MutBot at {base_url} — {e}", file=sys.stderr)
        sys.exit(1)

    # 2. 触发重启
    try:
        req = urllib.request.Request(f"{base_url}/api/restart", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            status = result.get("status", "unknown")
            if status == "already_restarting":
                print("Restart already in progress.")
                # 仍然等待完成
            elif status == "restarting":
                print("Restart triggered.")
            else:
                print(f"Unexpected response: {result}", file=sys.stderr)
                sys.exit(1)
    except Exception as e:
        print(f"Error: Failed to trigger restart — {e}", file=sys.stderr)
        sys.exit(1)

    # 3. 等待新 Worker 就绪（generation 变化）
    print("Waiting for new Worker...", end="", flush=True)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        time.sleep(1.0)
        print(".", end="", flush=True)
        try:
            req = urllib.request.Request(f"{base_url}/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                health = json.loads(resp.read())
                new_gen = health.get("worker", {}).get("generation", 0)
                restarting = health.get("restarting", False)
                if new_gen > old_gen and not restarting:
                    worker = health.get("worker", {})
                    print(f"\nRestart complete. Worker gen={new_gen} pid={worker.get('pid')} ready.")
                    return
        except Exception:
            pass

    print("\nTimeout waiting for restart to complete.", file=sys.stderr)
    sys.exit(1)


def _pysandbox_dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """向活 mutbot server 的 MCP endpoint 提交 Python 代码执行。

    复用 mutagent 的 :class:`PysandboxClient`，只传品牌参数。
    """
    from mutagent.cli.pysandbox import PysandboxClient

    if args.code is not None and args.script is not None:
        parser.error("-c CODE and script file are mutually exclusive")

    PysandboxClient(
        prog="mutbot",
        default_url=f"http://127.0.0.1:{args.port}/mcp",
        unreachable_hint=(
            "To start:           mutbot serve\n"
            "Read logs offline:  tail -100 ~/.mutbot/logs/server-*.log"
        ),
    ).dispatch(args)


def _build_top_parser() -> argparse.ArgumentParser:
    """构建顶层 argparser，所有子命令在此统一注册。"""
    import mutbot

    parser = argparse.ArgumentParser(
        prog="mutbot",
        description="MutBot — AI-powered Web UI with Python sandbox.",
    )
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"mutbot {mutbot.__version__}",
    )

    sub = parser.add_subparsers(dest="command", title="commands", metavar="COMMAND")
    sub.required = False
    parser.set_defaults(command="serve")

    # ---- serve（默认） ----
    serve_p = sub.add_parser("serve", help="Start the server (default)")
    serve_p.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging to console",
    )
    serve_p.add_argument(
        "--no-supervisor", action="store_true",
        help="Run in single-process mode (bypass Supervisor)",
    )
    serve_p.add_argument(
        "--listen", action="append", default=None, metavar="[HOST:]PORT",
        help="Bind address (repeatable). Default: 127.0.0.1:8741",
    )

    # ---- worker（内部） ----
    worker_p = sub.add_parser("worker", help="Run as Worker process (internal)")
    worker_p.add_argument(
        "--port", type=int, required=True,
        help="Worker listen port",
    )
    worker_p.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging",
    )

    # ---- restart ----
    restart_p = sub.add_parser("restart", help="Trigger hot restart of running server")
    restart_p.add_argument(
        "--port", type=int, default=8741,
        help="Server port (default: 8741)",
    )

    # ---- pysandbox ----
    pys_p = sub.add_parser(
        "pysandbox",
        help="Run Python code in server's sandbox",
        description="Run Python code in the running mutbot server's sandbox.",
    )
    pys_p.add_argument(
        "-c", dest="code", metavar="CODE",
        help="Code string (like python -c)",
    )
    pys_p.add_argument(
        "script", nargs="?",
        help="Script file, or '-' to read from stdin",
    )
    pys_p.add_argument(
        "--port", type=int, default=8741,
        help="Server port (default: 8741)",
    )
    pys_p.add_argument(
        "--timeout", type=float, default=30.0,
        help="RPC timeout in seconds (default: 30.0)",
    )

    return parser


def main() -> None:
    """入口函数 — console_scripts 和 python -m 共用。"""
    import os
    from mutbot.runtime import storage
    storage.STARTUP_CWD = os.getcwd()

    parser = _build_top_parser()
    args = parser.parse_args()

    if args.command == "serve":
        from mutbot.web.server import run_server
        run_server(args)

    elif args.command == "worker":
        from mutbot.web.server import worker_main
        worker_main(port=args.port, debug=args.debug)

    elif args.command == "restart":
        _restart_command(args.port)

    elif args.command == "pysandbox":
        _pysandbox_dispatch(args, parser)

    else:
        # argparse 保证不会走到这里（subparsers.required=False + default="serve"）
        parser.print_help()


if __name__ == "__main__":
    main()
