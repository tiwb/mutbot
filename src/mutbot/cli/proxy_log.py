"""mutbot.cli.proxy_log -- 代理日志查询 CLI。

用法：
    python -m mutbot.cli.proxy_log summary [--date DATE]
    python -m mutbot.cli.proxy_log list [--model MODEL] [-n LIMIT]
    python -m mutbot.cli.proxy_log usage [--date DATE]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from mutbot.proxy.logging import DEFAULT_LOG_DIR, get_summary, read_log_file


def cmd_summary(args: argparse.Namespace) -> None:
    """显示日志摘要。"""
    date_str = _resolve_date(args.date)
    summary = get_summary(date_str, Path(args.log_dir))

    if summary["total_calls"] == 0:
        print(f"No proxy calls recorded on {date_str}")
        return

    print(f"Date: {summary['date']}")
    print(f"Total calls: {summary['total_calls']}")
    print(f"Input tokens: {summary['total_input_tokens']:,}")
    print(f"Output tokens: {summary['total_output_tokens']:,}")
    print(f"Avg duration: {summary['avg_duration_ms']}ms")
    print(f"By model:")
    for model, count in summary["by_model"].items():
        print(f"  {model}: {count}")


def cmd_list(args: argparse.Namespace) -> None:
    """列出最近的代理调用。"""
    date_str = _resolve_date(args.date)
    records = read_log_file(date_str, Path(args.log_dir))

    if args.model:
        records = [r for r in records if r.get("model") == args.model]

    # 取最后 N 条
    records = records[-args.n:]

    if not records:
        print(f"No matching proxy calls on {date_str}")
        return

    for r in records:
        ts = r.get("ts", "")[:19]
        model = r.get("model", "?")
        duration = r.get("duration_ms", 0)
        usage = r.get("usage", {})
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        print(f"  {ts}  {model:<30s}  {in_tok:>6d}→{out_tok:<6d}  {duration:>5d}ms")


def cmd_usage(args: argparse.Namespace) -> None:
    """显示 token 使用统计。"""
    date_str = _resolve_date(args.date)
    records = read_log_file(date_str, Path(args.log_dir))

    if not records:
        print(f"No proxy calls on {date_str}")
        return

    # 按模型统计
    by_model: dict[str, dict[str, int]] = {}
    for r in records:
        model = r.get("model", "unknown")
        usage = r.get("usage", {})
        if model not in by_model:
            by_model[model] = {"calls": 0, "input": 0, "output": 0}
        by_model[model]["calls"] += 1
        by_model[model]["input"] += usage.get("input_tokens", 0)
        by_model[model]["output"] += usage.get("output_tokens", 0)

    print(f"Usage for {date_str}:")
    print(f"{'Model':<30s}  {'Calls':>6s}  {'Input':>10s}  {'Output':>10s}")
    print("-" * 62)
    for model, stats in sorted(by_model.items()):
        print(
            f"{model:<30s}  {stats['calls']:>6d}  "
            f"{stats['input']:>10,d}  {stats['output']:>10,d}"
        )


def _resolve_date(date_str: str) -> str:
    """解析日期字符串，支持 'today'。"""
    if date_str == "today":
        return datetime.now().strftime("%Y-%m-%d")
    return date_str


def main() -> None:
    parser = argparse.ArgumentParser(description="Proxy log query tool")
    parser.add_argument(
        "--log-dir", default=str(DEFAULT_LOG_DIR),
        help="Log directory path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # summary
    p_summary = subparsers.add_parser("summary", help="Show daily summary")
    p_summary.add_argument("--date", default="today", help="Date (YYYY-MM-DD or 'today')")

    # list
    p_list = subparsers.add_parser("list", help="List recent calls")
    p_list.add_argument("--date", default="today", help="Date (YYYY-MM-DD or 'today')")
    p_list.add_argument("--model", default="", help="Filter by model")
    p_list.add_argument("-n", type=int, default=20, help="Number of entries")

    # usage
    p_usage = subparsers.add_parser("usage", help="Show token usage")
    p_usage.add_argument("--date", default="today", help="Date (YYYY-MM-DD or 'today')")

    args = parser.parse_args()

    commands = {
        "summary": cmd_summary,
        "list": cmd_list,
        "usage": cmd_usage,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
