"""MutBot entry point: python -m mutbot [command]

支持：
  python -m mutbot              — 启动服务器（默认 Supervisor 模式）
  python -m mutbot restart      — 触发运行中服务器的热重启
  python -m mutbot --worker     — Worker 模式（仅 Supervisor 内部调用）
  python -m mutbot --no-supervisor — 单进程模式（调试用）
"""

import sys


def _restart_command() -> None:
    """发送 POST /api/restart 到运行中的 Supervisor，等待新 Worker 就绪。"""
    import json
    import time
    import urllib.request

    # 解析参数
    port = 8741
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

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


def _log_command() -> None:
    """Delegate to mutbot.cli.log_query with remaining argv."""
    from mutbot.cli.log_query import main as log_main
    log_main(sys.argv[2:])


def main() -> None:
    """入口函数 — console_scripts 和 python -m 共用。"""
    if len(sys.argv) > 1 and sys.argv[1] == "restart":
        _restart_command()
    elif len(sys.argv) > 1 and sys.argv[1] == "log":
        _log_command()
    else:
        from mutbot.web.server import main as server_main
        server_main()


if __name__ == "__main__":
    main()
