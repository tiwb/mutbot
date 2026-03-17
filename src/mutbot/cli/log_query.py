"""mutbot.cli.log_query -- mutbot 日志查询 CLI。

用法：
    python -m mutbot log sessions [--last N] [--since DURATION] [--all]
    python -m mutbot log query [-s SESSION] [-p PATTERN] [-l LEVEL] [-n LIMIT] [-e] [--logger NAME] [--since DURATION]
    python -m mutbot log errors [-n LIMIT] [--since DURATION] [-e]
    python -m mutbot log tail [-l LEVEL] [-p PATTERN] [--logger NAME]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from mutagent.runtime.log_query import LogQueryEngine, SessionInfo

# mutbot standard log directory
LOG_DIR = Path.home() / ".mutbot" / "logs"
SESSIONS_DIR = Path.home() / ".mutbot" / "sessions"

# Duration pattern: 30m, 2h, 1d, 1h30m
_DURATION_RE = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")


def _parse_duration(s: str) -> timedelta | None:
    """Parse a duration string like '30m', '2h', '1d', '1h30m'."""
    m = _DURATION_RE.match(s.strip())
    if not m or not any(m.groups()):
        return None
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    return timedelta(days=days, hours=hours, minutes=minutes)


def _session_timestamp_to_datetime(ts: str) -> datetime | None:
    """Convert session timestamp prefix like '20260317_100445' to datetime.

    Handles prefixes: 'server-20260317_100445', 'session-20260317_100445-hexid'.
    """
    # Extract the YYYYMMDD_HHMMSS part
    m = re.search(r"(\d{8}_\d{6})", ts)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _load_session_metadata() -> dict[str, dict]:
    """Load all session metadata from ~/.mutbot/sessions/, keyed by session_id."""
    if not SESSIONS_DIR.is_dir():
        return {}
    import json

    result: dict[str, dict] = {}
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            sid = data.get("id", "")
            if sid:
                result[sid] = data
        except (json.JSONDecodeError, OSError):
            continue
    return result


def _extract_session_id_from_log(session_prefix: str) -> str | None:
    """Extract hex session ID from log filename prefix.

    'session-20260315_165708-f0112b381c14' -> 'f0112b381c14'
    """
    m = re.match(r"session-\d{8}_\d{6}-([0-9a-f]+)$", session_prefix)
    return m.group(1) if m else None


def _session_type_label(session_prefix: str) -> str:
    """Return a short type label for a session prefix."""
    if session_prefix.startswith("server-"):
        return "server"
    if session_prefix.startswith("supervisor-"):
        return "supervisor"
    if session_prefix.startswith("session-"):
        return "session"
    return "standalone"


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 0:
        return "-"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    hours = int(seconds / 3600)
    mins = int((seconds % 3600) / 60)
    if mins:
        return f"{hours}h{mins}m"
    return f"{hours}h"


def _match_session(
    sessions: list[SessionInfo],
    query: str,
    metadata: dict[str, dict],
) -> list[SessionInfo]:
    """Filter sessions by smart matching on query string.

    Priority:
    1. Session ID hex prefix match
    2. Session title fuzzy match (from metadata)
    3. Timestamp prefix match (e.g. '20260317')
    4. Type match ('server', 'terminal', 'agent')
    """
    if not query:
        return sessions

    q = query.lower()
    results: list[SessionInfo] = []

    for s in sessions:
        # 1. Hex session ID match
        hex_id = _extract_session_id_from_log(s.timestamp)
        if hex_id and hex_id.startswith(q):
            results.append(s)
            continue

        # 2. Title match from metadata
        if hex_id and hex_id in metadata:
            title = metadata[hex_id].get("title", "").lower()
            sess_type = metadata[hex_id].get("type", "").lower()
            if q in title or q in sess_type:
                results.append(s)
                continue

        # 3. Timestamp prefix match
        if q in s.timestamp.lower():
            results.append(s)
            continue

        # 4. Type match
        type_label = _session_type_label(s.timestamp)
        if q in type_label:
            results.append(s)
            continue

    return results


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_sessions(args: argparse.Namespace) -> None:
    """List log sessions with metadata enrichment."""
    engine = LogQueryEngine(LOG_DIR)
    sessions = engine.list_sessions()

    if not sessions:
        print("No log sessions found in", LOG_DIR)
        return

    # Apply --since filter
    if args.since:
        delta = _parse_duration(args.since)
        if delta is None:
            print(f"Invalid duration: {args.since}", file=sys.stderr)
            sys.exit(1)
        cutoff = datetime.now() - delta
        sessions = [
            s for s in sessions
            if (_session_timestamp_to_datetime(s.timestamp) or datetime.min) >= cutoff
        ]

    # Load metadata for session enrichment
    metadata = _load_session_metadata()

    # Apply smart filter if provided
    if args.filter:
        sessions = _match_session(sessions, args.filter, metadata)

    if not sessions:
        print("No matching sessions found.")
        return

    # Apply --last (default 10 unless --all)
    if not args.all:
        last_n = args.last or 10
        sessions = sessions[-last_n:]

    # Print table
    # Header
    print(
        f"{'Session':<48s}  {'Title/Info':<24s}  "
        f"{'Logs':>5s}  {'Duration':>8s}"
    )
    print("-" * 93)

    for s in sessions:
        # Enrich with metadata
        hex_id = _extract_session_id_from_log(s.timestamp)
        info = ""
        if hex_id and hex_id in metadata:
            meta = metadata[hex_id]
            title = meta.get("title", "")
            # Get short type: 'mutbot.session.AgentSession' -> 'Agent'
            sess_type = meta.get("type", "")
            short_type = sess_type.rsplit(".", 1)[-1].replace("Session", "")
            if title and short_type:
                info = f"{short_type}: {title}"
            elif title:
                info = title
        else:
            type_label = _session_type_label(s.timestamp)
            if type_label != "session":
                info = f"({type_label})"

        log_count = str(s.log_lines) if s.log_lines >= 0 else "-"
        duration = _format_duration(s.duration_seconds)

        # Truncate info for display
        if len(info) > 24:
            info = info[:22] + ".."

        print(
            f"{s.timestamp:<48s}  {info:<24s}  "
            f"{log_count:>5s}  {duration:>8s}"
        )

    print(f"\n({len(sessions)} sessions shown)")


def cmd_query(args: argparse.Namespace) -> None:
    """Query log entries."""
    engine = LogQueryEngine(LOG_DIR)

    # Resolve session via smart matching
    session = _resolve_session_arg(engine, args.session)

    entries = engine.query_logs(
        session=session,
        pattern=args.pattern or "",
        level=args.level or "DEBUG",
        limit=args.n,
        logger_name=args.logger or "",
    )

    if not entries:
        print("No matching log entries.")
        return

    for entry in entries:
        if args.expand:
            # Show full multiline content
            level_color = _level_prefix(entry.level)
            print(f"{entry.timestamp} {level_color} {entry.logger_name} - {entry.message}")
        else:
            # Single line, truncate message
            msg = entry.message.split("\n")[0]
            if len(msg) > 120:
                msg = msg[:118] + ".."
            level_color = _level_prefix(entry.level)
            print(f"{entry.timestamp} {level_color} {entry.logger_name} - {msg}")


def cmd_errors(args: argparse.Namespace) -> None:
    """Show recent errors and warnings."""
    engine = LogQueryEngine(LOG_DIR)

    # If --since, find sessions within range and query across them
    session = ""
    if args.since:
        delta = _parse_duration(args.since)
        if delta is None:
            print(f"Invalid duration: {args.since}", file=sys.stderr)
            sys.exit(1)
        cutoff = datetime.now() - delta
        sessions = engine.list_sessions()
        recent = [
            s for s in sessions
            if (_session_timestamp_to_datetime(s.timestamp) or datetime.min) >= cutoff
        ]
        # Query each recent session
        all_entries = []
        for s in recent:
            entries = engine.query_logs(
                session=s.timestamp,
                level="WARNING",
                limit=args.n,
            )
            all_entries.extend(entries)
        # Sort by timestamp and limit
        all_entries.sort(key=lambda e: e.timestamp)
        all_entries = all_entries[-args.n:]

        if not all_entries:
            print("No errors/warnings found.")
            return

        for entry in all_entries:
            if args.expand:
                print(f"{entry.timestamp} {_level_prefix(entry.level)} {entry.logger_name} - {entry.message}")
            else:
                msg = entry.message.split("\n")[0]
                if len(msg) > 120:
                    msg = msg[:118] + ".."
                print(f"{entry.timestamp} {_level_prefix(entry.level)} {entry.logger_name} - {msg}")
        return

    # Default: latest session
    entries = engine.query_logs(
        session=session,
        level="WARNING",
        limit=args.n,
    )

    if not entries:
        print("No errors/warnings found.")
        return

    for entry in entries:
        if args.expand:
            print(f"{entry.timestamp} {_level_prefix(entry.level)} {entry.logger_name} - {entry.message}")
        else:
            msg = entry.message.split("\n")[0]
            if len(msg) > 120:
                msg = msg[:118] + ".."
            print(f"{entry.timestamp} {_level_prefix(entry.level)} {entry.logger_name} - {msg}")


def cmd_tail(args: argparse.Namespace) -> None:
    """Follow log output in real-time."""
    engine = LogQueryEngine(LOG_DIR)

    # Find the latest log file
    log_file = engine._find_latest_file(".log")
    if log_file is None:
        print("No log files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Tailing {log_file.name} (Ctrl+C to stop)")
    print("-" * 60)

    import logging as _logging

    min_level = _logging.getLevelNamesMapping().get(
        (args.level or "DEBUG").upper(), _logging.DEBUG
    )
    compiled = re.compile(args.pattern) if args.pattern else None
    logger_filter = args.logger or ""

    try:
        with open(log_file, encoding="utf-8") as f:
            # Seek to end
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.2)
                    continue
                line = line.rstrip("\n\r")
                if not line:
                    continue

                # Quick parse for filtering
                from mutagent.runtime.log_query import _LOG_LINE_RE

                m = _LOG_LINE_RE.match(line)
                if m:
                    level_name = m.group(2)
                    logger_name = m.group(3)
                    message = m.group(4)

                    level_val = _logging.getLevelNamesMapping().get(level_name, 0)
                    if level_val < min_level:
                        continue
                    if logger_filter:
                        if logger_name != logger_filter and not logger_name.startswith(logger_filter + "."):
                            continue
                    if compiled and not compiled.search(message):
                        continue

                print(line)
    except KeyboardInterrupt:
        print("\nStopped.")


def _resolve_session_arg(engine: LogQueryEngine, session_arg: str | None) -> str:
    """Resolve --session argument to a session timestamp using smart matching."""
    if not session_arg:
        return ""  # LogQueryEngine defaults to latest

    # Try direct use (it may be an exact timestamp or prefix)
    # Also try smart matching
    sessions = engine.list_sessions()
    metadata = _load_session_metadata()
    matched = _match_session(sessions, session_arg, metadata)

    if matched:
        return matched[-1].timestamp  # Latest match
    # Fall back to passing through (LogQueryEngine handles partial matching)
    return session_arg


def _level_prefix(level: str) -> str:
    """Return a fixed-width level prefix."""
    return f"{level:<8s}"


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mutbot log",
        description="mutbot log query tool",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # sessions
    p_sessions = subparsers.add_parser("sessions", help="List log sessions")
    p_sessions.add_argument("--last", type=int, default=0, help="Show last N sessions (default: 10)")
    p_sessions.add_argument("--since", default="", help="Time range filter (e.g. 2h, 1d, 30m)")
    p_sessions.add_argument("--all", action="store_true", help="Show all sessions")
    p_sessions.add_argument("filter", nargs="?", default="", help="Filter: session ID, title, type, or timestamp")

    # query
    p_query = subparsers.add_parser("query", help="Query log entries")
    p_query.add_argument("-s", "--session", default="", help="Session ID, title, or timestamp")
    p_query.add_argument("-p", "--pattern", default="", help="Regex pattern to match messages")
    p_query.add_argument("-l", "--level", default="DEBUG", help="Minimum log level")
    p_query.add_argument("-n", type=int, default=50, help="Max entries (default: 50)")
    p_query.add_argument("-e", "--expand", action="store_true", help="Expand multiline entries")
    p_query.add_argument("--logger", default="", help="Filter by logger name (prefix match)")

    # errors
    p_errors = subparsers.add_parser("errors", help="Show recent errors/warnings")
    p_errors.add_argument("-n", type=int, default=20, help="Max entries (default: 20)")
    p_errors.add_argument("--since", default="", help="Time range (e.g. 2h, 1d)")
    p_errors.add_argument("-e", "--expand", action="store_true", help="Expand tracebacks")

    # tail
    p_tail = subparsers.add_parser("tail", help="Follow log output in real-time")
    p_tail.add_argument("-l", "--level", default="DEBUG", help="Minimum log level")
    p_tail.add_argument("-p", "--pattern", default="", help="Regex pattern filter")
    p_tail.add_argument("--logger", default="", help="Filter by logger name")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "sessions": cmd_sessions,
        "query": cmd_query,
        "errors": cmd_errors,
        "tail": cmd_tail,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
