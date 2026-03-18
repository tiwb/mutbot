"""mutbot.cli.cdp_debug -- Chrome CDP 远程调试 CLI。

用法：
    python -m mutbot.cli.cdp_debug pages                          # 列出可调试页面
    python -m mutbot.cli.cdp_debug eval "location.href"           # 执行 JS 表达式
    python -m mutbot.cli.cdp_debug console --reload --filter mutbot  # 监听 console
    python -m mutbot.cli.cdp_debug check --marker ensureLandingInHistory  # 一键诊断
    python -m mutbot.cli.cdp_debug errors                         # 刷新页面并捕获异常/错误
    python -m mutbot.cli.cdp_debug errors --no-reload             # 不刷新，仅监听异常

前提：Chrome 需以 --remote-debugging-port=9222 启动：
    chrome.exe --remote-debugging-port=9222 --user-data-dir=C:/tmp/chrome-debug
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import struct
import sys
import urllib.request
from base64 import b64encode
from typing import Any


# ---------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455 text frames only, no external deps)
# ---------------------------------------------------------------------------

class SimpleWebSocket:
    """Minimal synchronous WebSocket client for CDP communication."""

    def __init__(self, url: str) -> None:
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)$", url)
        if not m:
            raise ValueError(f"Invalid ws:// URL: {url}")
        self.host = m.group(1)
        self.port = int(m.group(2))
        self.path = m.group(3)
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        key = b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed")
            resp += chunk
        if b"101" not in resp.split(b"\r\n")[0]:
            first_line = resp.split(b'\r\n')[0].decode()
            raise ConnectionError(f"WebSocket upgrade rejected: {first_line}")

    def send(self, data: str) -> None:
        assert self.sock is not None
        payload = data.encode()
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        # Build frame: FIN + TEXT opcode
        frame = bytearray()
        frame.append(0x81)  # FIN + text
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)  # MASK bit set
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack(">Q", length))
        frame.extend(mask_key)
        frame.extend(masked)
        self.sock.sendall(frame)

    def recv(self, timeout: float | None = None) -> str:
        assert self.sock is not None
        if timeout is not None:
            self.sock.settimeout(timeout)
        else:
            self.sock.settimeout(30)

        def _read_exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = self.sock.recv(n - len(buf))  # type: ignore[union-attr]
                if not chunk:
                    raise ConnectionError("Connection closed")
                buf += chunk
            return buf

        header = _read_exact(2)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", _read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _read_exact(8))[0]

        if masked:
            mask_key = _read_exact(4)
            data = _read_exact(length)
            data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        else:
            data = _read_exact(length)

        if opcode == 0x8:  # close
            raise ConnectionError("WebSocket closed by server")
        if opcode == 0x9:  # ping → pong
            self._send_pong(data)
            return self.recv(timeout)

        return data.decode()

    def _send_pong(self, data: bytes) -> None:
        assert self.sock is not None
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        frame = bytearray()
        frame.append(0x8A)  # FIN + pong
        frame.append(0x80 | len(data))
        frame.extend(mask_key)
        frame.extend(masked)
        self.sock.sendall(frame)

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self) -> SimpleWebSocket:
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

def get_pages(port: int = 9222) -> list[dict[str, Any]]:
    """GET /json to list debuggable pages."""
    url = f"http://localhost:{port}/json"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        print(f"Error: Cannot connect to Chrome CDP on port {port}.", file=sys.stderr)
        print(f"Make sure Chrome is running with --remote-debugging-port={port}", file=sys.stderr)
        print(f"  chrome.exe --remote-debugging-port={port} --user-data-dir=C:/tmp/chrome-debug", file=sys.stderr)
        sys.exit(1)


def find_page(pages: list[dict[str, Any]], *, index: int | None = None, url_pattern: str = "") -> dict[str, Any]:
    """Select a page by index or URL pattern."""
    if not pages:
        print("Error: No debuggable pages found.", file=sys.stderr)
        sys.exit(1)

    # Filter to only 'page' type
    page_list = [p for p in pages if p.get("type") == "page"]
    if not page_list:
        page_list = pages

    if url_pattern:
        matched = [p for p in page_list if url_pattern.lower() in p.get("url", "").lower()]
        if not matched:
            print(f"Error: No page matching '{url_pattern}'.", file=sys.stderr)
            print("Available pages:", file=sys.stderr)
            for i, p in enumerate(page_list):
                print(f"  [{i}] {p.get('url', '?')}", file=sys.stderr)
            sys.exit(1)
        return matched[0]

    idx = index if index is not None else 0
    if idx >= len(page_list):
        print(f"Error: Page index {idx} out of range (0-{len(page_list)-1}).", file=sys.stderr)
        sys.exit(1)
    return page_list[idx]


def cdp_call(ws: SimpleWebSocket, method: str, params: dict[str, Any] | None = None,
             msg_id: int = 1) -> dict[str, Any]:
    """Send a CDP command and wait for the response."""
    msg: dict[str, Any] = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    ws.send(json.dumps(msg))

    while True:
        raw = ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == msg_id:
            if "error" in resp:
                raise RuntimeError(f"CDP error: {resp['error']}")
            return resp.get("result", {})
        # Skip events while waiting for response


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_pages(args: argparse.Namespace) -> None:
    """List debuggable pages."""
    pages = get_pages(args.port)
    print(f"{'#':<4s} {'TYPE':<12s} {'URL'}")
    print("-" * 70)
    for i, p in enumerate(pages):
        ptype = p.get("type", "?")
        url = p.get("url", "?")
        print(f"{i:<4d} {ptype:<12s} {url}")


def cmd_eval(args: argparse.Namespace) -> None:
    """Evaluate JS expression in target page."""
    pages = get_pages(args.port)
    page = find_page(pages, index=args.page, url_pattern=args.url or "")
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        print("Error: Page has no webSocketDebuggerUrl.", file=sys.stderr)
        sys.exit(1)

    print(f"Target: {page.get('url', '?')}", file=sys.stderr)

    with SimpleWebSocket(ws_url) as ws:
        params: dict[str, Any] = {
            "expression": args.expression,
            "returnByValue": True,
        }
        if getattr(args, "await_promise", False):
            params["awaitPromise"] = True
        result = cdp_call(ws, "Runtime.evaluate", params)
        value = result.get("result", {})
        if value.get("type") == "undefined":
            print("undefined")
        elif "value" in value:
            v = value["value"]
            if isinstance(v, str):
                print(v)
            else:
                print(json.dumps(v, indent=2, ensure_ascii=False))
        else:
            print(json.dumps(value, indent=2, ensure_ascii=False))


def cmd_console(args: argparse.Namespace) -> None:
    """Listen to console output from target page."""
    pages = get_pages(args.port)
    page = find_page(pages, index=args.page, url_pattern=args.url or "")
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        print("Error: Page has no webSocketDebuggerUrl.", file=sys.stderr)
        sys.exit(1)

    print(f"Target: {page.get('url', '?')}", file=sys.stderr)
    filter_pattern = args.filter.lower() if args.filter else None

    with SimpleWebSocket(ws_url) as ws:
        # Enable Runtime domain for console events
        cdp_call(ws, "Runtime.enable", msg_id=1)
        print("Listening for console messages... (Ctrl+C to stop)", file=sys.stderr)

        if args.reload:
            print("Reloading page (ignoreCache=true)...", file=sys.stderr)
            cdp_call(ws, "Page.reload", {"ignoreCache": True}, msg_id=2)

        timeout = args.timeout
        try:
            while True:
                try:
                    raw = ws.recv(timeout=timeout)
                except socket.timeout:
                    print(f"\nTimeout ({timeout}s) reached.", file=sys.stderr)
                    break

                msg = json.loads(raw)
                if msg.get("method") == "Runtime.consoleAPICalled":
                    params = msg["params"]
                    level = params.get("type", "log")
                    call_args = params.get("args", [])
                    parts = []
                    for a in call_args:
                        if "value" in a:
                            parts.append(str(a["value"]))
                        elif a.get("type") == "object" and "description" in a:
                            parts.append(a["description"])
                        else:
                            parts.append(json.dumps(a))
                    text = " ".join(parts)

                    if filter_pattern and filter_pattern not in text.lower():
                        continue
                    print(f"[{level:>7s}] {text}")

        except KeyboardInterrupt:
            print("\nStopped.", file=sys.stderr)


def cmd_check(args: argparse.Namespace) -> None:
    """One-shot diagnostic check for the target page."""
    pages = get_pages(args.port)
    page = find_page(pages, index=args.page, url_pattern=args.url or "")
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        print("Error: Page has no webSocketDebuggerUrl.", file=sys.stderr)
        sys.exit(1)

    print(f"=== CDP Diagnostic Check ===")
    print(f"Target: {page.get('url', '?')}")
    print()

    with SimpleWebSocket(ws_url) as ws:
        # 1. Page state
        state = cdp_call(ws, "Runtime.evaluate", {
            "expression": "JSON.stringify({href: location.href, hash: location.hash, historyLength: history.length, title: document.title})",
            "returnByValue": True,
        })
        state_val = state.get("result", {}).get("value", "{}")
        if isinstance(state_val, str):
            state_obj = json.loads(state_val)
        else:
            state_obj = state_val
        print("Page state:")
        for k, v in state_obj.items():
            print(f"  {k}: {v}")
        print()

        # 2. Loaded JS resources
        res = cdp_call(ws, "Runtime.evaluate", {
            "expression": """JSON.stringify(
                performance.getEntriesByType('resource')
                    .filter(r => r.initiatorType === 'script' || r.name.endsWith('.js'))
                    .map(r => ({name: r.name, size: r.transferSize}))
            )""",
            "returnByValue": True,
        })
        res_val = res.get("result", {}).get("value", "[]")
        if isinstance(res_val, str):
            js_files = json.loads(res_val)
        else:
            js_files = res_val
        print(f"Loaded JS files ({len(js_files)}):")
        for f in js_files:
            name = f.get("name", "?")
            size = f.get("size", 0)
            print(f"  {name}  ({size:,} bytes)")
        print()

        # 3. Marker check
        marker = args.marker
        if marker:
            # Check via fetching the main JS and searching
            # First, find the main entry JS URL
            main_js = None
            for f in js_files:
                name = f.get("name", "")
                if "index" in name.lower() or "main" in name.lower():
                    main_js = name
                    break
            if not main_js and js_files:
                main_js = js_files[0].get("name")

            if main_js:
                check_expr = f"""
                    fetch("{main_js}").then(r => r.text()).then(t => {{
                        const found = t.includes("{marker}");
                        const idx = found ? t.indexOf("{marker}") : -1;
                        const ctx = found ? t.substring(Math.max(0, idx-30), idx+{len(marker)}+30) : "";
                        return JSON.stringify({{found, context: ctx}});
                    }})
                """
                marker_res = cdp_call(ws, "Runtime.evaluate", {
                    "expression": check_expr,
                    "returnByValue": True,
                    "awaitPromise": True,
                })
                marker_val = marker_res.get("result", {}).get("value", "{}")
                if isinstance(marker_val, str):
                    marker_obj = json.loads(marker_val)
                else:
                    marker_obj = marker_val
                found = marker_obj.get("found", False)
                ctx = marker_obj.get("context", "")
                symbol = "OK" if found else "MISSING"
                print(f"Marker '{marker}': {symbol}")
                if ctx:
                    print(f"  Context: ...{ctx}...")
            else:
                print(f"Marker '{marker}': No JS file found to check")
            print()

        # 4. DOM check
        dom = cdp_call(ws, "Runtime.evaluate", {
            "expression": """JSON.stringify({
                scripts: document.querySelectorAll('script[src]').length,
                appDiv: !!document.getElementById('app'),
                rootDiv: !!document.getElementById('root'),
                appMode: document.documentElement.classList.contains('app-mode')
            })""",
            "returnByValue": True,
        })
        dom_val = dom.get("result", {}).get("value", "{}")
        if isinstance(dom_val, str):
            dom_obj = json.loads(dom_val)
        else:
            dom_obj = dom_val
        print("DOM state:")
        for k, v in dom_obj.items():
            print(f"  {k}: {v}")


# Navigation events that indicate user action
_NAV_EVENTS = {
    "Page.frameStartedNavigating",
    "Page.navigatedWithinDocument",
    "Page.frameNavigated",
}


def cmd_wait(args: argparse.Namespace) -> None:
    """Wait for a navigation event, then dump state."""
    pages = get_pages(args.port)
    page = find_page(pages, index=args.page, url_pattern=args.url or "")
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        print("Error: Page has no webSocketDebuggerUrl.", file=sys.stderr)
        sys.exit(1)

    print(f"Target: {page.get('url', '?')}", file=sys.stderr)
    print("Waiting for navigation... (do your action in Chrome)", file=sys.stderr)

    console_log: list[str] = []

    with SimpleWebSocket(ws_url) as ws:
        cdp_call(ws, "Page.enable", msg_id=1)
        cdp_call(ws, "Runtime.enable", msg_id=2)

        timeout = args.timeout or 120
        trigger_event = ""
        trigger_url = ""

        try:
            while True:
                try:
                    raw = ws.recv(timeout=timeout)
                except socket.timeout:
                    print(f"\nTimeout ({timeout}s) — no navigation detected.", file=sys.stderr)
                    break

                msg = json.loads(raw)
                method = msg.get("method", "")

                # Capture console output during wait
                if method == "Runtime.consoleAPICalled":
                    call_args = msg["params"].get("args", [])
                    level = msg["params"].get("type", "log")
                    parts = []
                    for a in call_args:
                        if "value" in a:
                            parts.append(str(a["value"]))
                        elif a.get("type") == "object" and "description" in a:
                            parts.append(a["description"])
                        else:
                            parts.append(json.dumps(a))
                    text = " ".join(parts)
                    console_log.append(f"[{level:>7s}] {text}")
                    continue

                # Check for navigation events
                if method in _NAV_EVENTS:
                    params = msg.get("params", {})
                    trigger_event = method
                    trigger_url = params.get("url", params.get("frame", {}).get("url", ""))
                    # Collect remaining events for a short window
                    ws.sock.settimeout(1)  # type: ignore[union-attr]
                    try:
                        while True:
                            raw2 = ws.recv(timeout=1)
                            msg2 = json.loads(raw2)
                            m2 = msg2.get("method", "")
                            if m2 == "Runtime.consoleAPICalled":
                                ca = msg2["params"].get("args", [])
                                lv = msg2["params"].get("type", "log")
                                tx = " ".join(str(a.get("value", a)) for a in ca)
                                console_log.append(f"[{lv:>7s}] {tx}")
                            elif m2 in _NAV_EVENTS:
                                p2 = msg2.get("params", {})
                                u2 = p2.get("url", p2.get("frame", {}).get("url", ""))
                                if u2:
                                    trigger_url = u2
                    except (socket.timeout, ConnectionError, OSError):
                        pass
                    break

        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)

        # Output results
        print(f"Event: {trigger_event}")
        print(f"URL: {trigger_url}")

        if console_log:
            print(f"\nConsole ({len(console_log)} messages):")
            for line in console_log:
                print(f"  {line}")

        # Try to get final page state
        print()
        try:
            state = cdp_call(ws, "Runtime.evaluate", {
                "expression": "JSON.stringify({href: location.href, hash: location.hash, historyLength: history.length})",
                "returnByValue": True,
            }, msg_id=99)
            val = state.get("result", {}).get("value", "{}")
            state_obj = json.loads(val) if isinstance(val, str) else val
            print("Final state:")
            for k, v in state_obj.items():
                print(f"  {k}: {v}")
        except (ConnectionError, OSError, RuntimeError):
            # Page navigated away (cross-origin), check pages list
            print("Page navigated away (connection lost). Current pages:")
            try:
                for i, p in enumerate(get_pages(args.port)):
                    if p.get("type") == "page":
                        print(f"  [{i}] {p.get('url', '?')}")
            except Exception:
                print("  (unable to query)")


def cmd_errors(args: argparse.Namespace) -> None:
    """Reload page and capture runtime exceptions + error logs."""
    pages = get_pages(args.port)
    page = find_page(pages, index=args.page, url_pattern=args.url or "")
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        print("Error: Page has no webSocketDebuggerUrl.", file=sys.stderr)
        sys.exit(1)

    print(f"Target: {page.get('url', '?')}", file=sys.stderr)

    with SimpleWebSocket(ws_url) as ws:
        # Enable domains for exception and log capture
        cdp_call(ws, "Runtime.enable", msg_id=1)
        cdp_call(ws, "Log.enable", msg_id=2)

        reload = not args.no_reload
        if reload:
            print("Reloading page (ignoreCache=true)...", file=sys.stderr)
            cdp_call(ws, "Page.reload", {"ignoreCache": True}, msg_id=3)

        print("Listening for errors... (Ctrl+C to stop)", file=sys.stderr)

        timeout = args.timeout
        error_count = 0
        try:
            while True:
                try:
                    raw = ws.recv(timeout=timeout)
                except socket.timeout:
                    print(f"\nTimeout ({timeout}s) reached.", file=sys.stderr)
                    break

                msg = json.loads(raw)
                method = msg.get("method", "")

                if method == "Runtime.exceptionThrown":
                    error_count += 1
                    exc = msg["params"]["exceptionDetails"]
                    text = exc.get("text", "")
                    desc = exc.get("exception", {}).get("description", "")
                    url = exc.get("url", "")
                    line = exc.get("lineNumber", "?")
                    col = exc.get("columnNumber", "?")
                    print(f"\n[EXCEPTION] {text}")
                    print(f"  at {url}:{line}:{col}")
                    if desc:
                        for dl in desc.split("\n")[:10]:
                            print(f"  {dl}")

                elif method == "Log.entryAdded":
                    entry = msg["params"]["entry"]
                    level = entry.get("level", "")
                    text = entry.get("text", "")
                    entry_url = entry.get("url", "")
                    if level == "error":
                        error_count += 1
                        loc = f" ({entry_url})" if entry_url else ""
                        print(f"[LOG.error] {text}{loc}")
                    elif level == "warning" and args.warnings:
                        loc = f" ({entry_url})" if entry_url else ""
                        print(f"[LOG.warn]  {text}{loc}")

                elif method == "Runtime.consoleAPICalled":
                    ctype = msg["params"].get("type", "")
                    if ctype in ("error", "warn" if args.warnings else "error"):
                        call_args = msg["params"].get("args", [])
                        parts = []
                        for a in call_args:
                            if "value" in a:
                                parts.append(str(a["value"]))
                            elif a.get("type") == "object" and "description" in a:
                                parts.append(a["description"])
                            else:
                                parts.append(json.dumps(a))
                        text = " ".join(parts)
                        print(f"[console.{ctype}] {text}")
                        if ctype == "error":
                            error_count += 1

        except KeyboardInterrupt:
            print("\nStopped.", file=sys.stderr)

        print(f"\n{error_count} error(s) captured.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chrome CDP remote debugging tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Start Chrome with remote debugging enabled first:\n"
            "  chrome.exe --remote-debugging-port=9222 --user-data-dir=C:/tmp/chrome-debug"
        ),
    )
    parser.add_argument("--port", type=int, default=9222, help="CDP port (default: 9222)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # pages
    subparsers.add_parser("pages", help="List debuggable pages")

    # eval
    p_eval = subparsers.add_parser("eval", help="Evaluate JS expression")
    p_eval.add_argument("expression", help="JavaScript expression to evaluate")
    p_eval.add_argument("--page", type=int, default=None, help="Page index (default: 0)")
    p_eval.add_argument("--url", default="", help="Select page by URL pattern")
    p_eval.add_argument("--await", dest="await_promise", action="store_true", help="Await promise result")

    # console
    p_console = subparsers.add_parser("console", help="Listen to console output")
    p_console.add_argument("--page", type=int, default=None, help="Page index (default: 0)")
    p_console.add_argument("--url", default="", help="Select page by URL pattern")
    p_console.add_argument("--reload", action="store_true", help="Reload page (ignoreCache) after connecting")
    p_console.add_argument("--filter", default="", help="Filter console messages by keyword")
    p_console.add_argument("--timeout", type=float, default=None, help="Auto-exit after N seconds")

    # check
    p_check = subparsers.add_parser("check", help="One-shot diagnostic check")
    p_check.add_argument("--page", type=int, default=None, help="Page index (default: 0)")
    p_check.add_argument("--url", default="", help="Select page by URL pattern")
    p_check.add_argument("--marker", default="", help="Search for marker string in loaded JS")

    # wait
    p_wait = subparsers.add_parser("wait", help="Wait for navigation event then dump state")
    p_wait.add_argument("--page", type=int, default=None, help="Page index (default: 0)")
    p_wait.add_argument("--url", default="", help="Select page by URL pattern")
    p_wait.add_argument("--timeout", type=float, default=120, help="Max wait time in seconds (default: 120)")

    # errors
    p_errors = subparsers.add_parser("errors", help="Reload page and capture exceptions/errors")
    p_errors.add_argument("--page", type=int, default=None, help="Page index (default: 0)")
    p_errors.add_argument("--url", default="", help="Select page by URL pattern")
    p_errors.add_argument("--no-reload", action="store_true", help="Don't reload, just listen")
    p_errors.add_argument("--warnings", action="store_true", help="Also capture warnings")
    p_errors.add_argument("--timeout", type=float, default=10, help="Auto-exit after N seconds (default: 10)")

    args = parser.parse_args()

    commands = {
        "pages": cmd_pages,
        "eval": cmd_eval,
        "console": cmd_console,
        "check": cmd_check,
        "wait": cmd_wait,
        "errors": cmd_errors,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
