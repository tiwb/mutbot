"""mutbot.proxy.logging -- 代理 API 调用日志记录。

将代理的 API 调用记录到 JSONL 文件，用于分析外部软件的 LLM 使用情况。
存储路径：.mutbot/logs/proxy/YYYY-MM-DD.jsonl
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认日志目录
DEFAULT_LOG_DIR = Path(".mutbot/logs/proxy")


class ProxyLogger:
    """代理 API 调用日志记录器。"""

    def __init__(self, log_dir: Path | str = DEFAULT_LOG_DIR):
        self.log_dir = Path(log_dir)
        self._current_date: str = ""
        self._file: Any = None

    def log_call(
        self,
        *,
        client_format: str,
        model: str,
        provider: str,
        request_meta: dict[str, Any],
        response_meta: dict[str, Any],
        usage: dict[str, int],
        duration_ms: int,
    ) -> None:
        """记录一次代理 API 调用。"""
        record = {
            "type": "proxy_call",
            "ts": datetime.now(timezone.utc).isoformat(),
            "client_format": client_format,
            "model": model,
            "provider": provider,
            "request": request_meta,
            "response": response_meta,
            "usage": usage,
            "duration_ms": duration_ms,
        }

        today = datetime.now().strftime("%Y-%m-%d")
        self._ensure_file(today)
        try:
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
        except Exception:
            logger.warning("Failed to write proxy log", exc_info=True)

    def _ensure_file(self, date_str: str) -> None:
        """确保日志文件打开且日期正确。"""
        if self._current_date == date_str and self._file is not None:
            return

        # 关闭旧文件
        if self._file is not None:
            self._file.close()

        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{date_str}.jsonl"
        self._file = open(path, "a", encoding="utf-8")
        self._current_date = date_str
        logger.info("Proxy log file: %s", path)

    def close(self) -> None:
        """关闭日志文件。"""
        if self._file is not None:
            self._file.close()
            self._file = None


def read_log_file(date_str: str, log_dir: Path = DEFAULT_LOG_DIR) -> list[dict]:
    """读取指定日期的日志文件。"""
    path = log_dir / f"{date_str}.jsonl"
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def get_summary(date_str: str, log_dir: Path = DEFAULT_LOG_DIR) -> dict[str, Any]:
    """获取指定日期的日志摘要。"""
    records = read_log_file(date_str, log_dir)
    if not records:
        return {"date": date_str, "total_calls": 0}

    total_input = sum(r.get("usage", {}).get("input_tokens", 0) for r in records)
    total_output = sum(r.get("usage", {}).get("output_tokens", 0) for r in records)
    total_duration = sum(r.get("duration_ms", 0) for r in records)

    # 按模型分组
    by_model: dict[str, int] = {}
    for r in records:
        model = r.get("model", "unknown")
        by_model[model] = by_model.get(model, 0) + 1

    return {
        "date": date_str,
        "total_calls": len(records),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_duration_ms": total_duration,
        "avg_duration_ms": total_duration // len(records) if records else 0,
        "by_model": by_model,
    }
