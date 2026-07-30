from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

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


def configure_logging(paths: RuntimePaths, level: int = logging.INFO) -> None:
    paths.ensure()
    root = logging.getLogger()
    if getattr(root, "_folder2feishu_configured", False):
        return
    root.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        "%Y-%m-%dT%H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        paths.logs / "folder2feishu.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
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
