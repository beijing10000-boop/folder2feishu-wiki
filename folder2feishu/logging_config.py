from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from .observability import request_id_var, stage_var, task_id_var
from .runtime import RuntimePaths

SENSITIVE_HEADERS = {"authorization", "x-app-secret", "cookie", "set-cookie"}


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for marker in ("Bearer ", "u-", "t-", "refresh_token=", "client_secret="):
            if marker in message:
                prefix, _, _ = message.partition(marker)
                record.msg = prefix + marker + "***"
                record.args = ()
                break
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line so a task can be traced without parsing prose."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "server_time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "") or request_id_var.get(),
            "task_id": getattr(record, "task_id", "") or task_id_var.get(),
            "stage": getattr(record, "stage", "") or stage_var.get(),
        }
        for key in (
            "user_id",
            "batch",
            "processed",
            "path",
            "duration_ms",
            "retry_count",
            "error_type",
            "action_id",
            "action_type",
            "result",
            "bytes",
            "remote_token",
        ):
            value = getattr(record, key, None)
            if value not in (None, ""):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(paths: RuntimePaths, level: int = logging.INFO) -> None:
    paths.ensure()
    root = logging.getLogger()
    if getattr(root, "_folder2feishu_configured", False):
        return
    root.setLevel(level)
    formatter = JsonFormatter()
    file_handler = RotatingFileHandler(
        paths.logs / "folder2feishu.log",
        # Keep enough local evidence for multiple large migration runs without
        # allowing logs to grow forever.
        maxBytes=20 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SecretRedactionFilter())
    root.addHandler(file_handler)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.addFilter(SecretRedactionFilter())
        root.addHandler(console)
    root.__dict__["_folder2feishu_configured"] = True
