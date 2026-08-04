"""Bounded, incremental access to the local structured application log."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_READ_BYTES = 256 * 1024
MAX_READ_BYTES = 1024 * 1024


def _is_user_visible(entry: dict[str, Any]) -> bool:
    """Keep remote activity and actionable messages, not browser polling noise."""

    level = str(entry.get("level") or "").upper()
    logger = str(entry.get("logger") or "")
    message = str(entry.get("message") or "")
    if level in {"WARNING", "ERROR", "CRITICAL"}:
        return True
    if logger == "httpx" and "open.feishu.cn" in message:
        return True
    return bool(entry.get("task_id")) and logger != "folder2feishu.api"


def read_runtime_logs(
    path: str | Path,
    *,
    after: int | None = None,
    limit: int = 100,
    max_bytes: int = DEFAULT_READ_BYTES,
) -> dict[str, Any]:
    """Read complete JSON log lines using a byte cursor.

    The first request tails the current file. Later requests continue from the
    returned cursor, so the browser never reloads a multi-megabyte log. A log
    rotation resets the cursor safely to the tail of the new file.
    """

    log_path = Path(path)
    limit = min(max(int(limit), 1), 500)
    max_bytes = min(max(int(max_bytes), 4 * 1024), MAX_READ_BYTES)
    if not log_path.is_file():
        return {"entries": [], "next_after": 0, "reset": bool(after)}

    size = log_path.stat().st_size
    reset = after is not None and (after < 0 or after > size)
    if after is None or reset:
        start = max(0, size - max_bytes)
        tailing = True
    else:
        start = int(after)
        tailing = False

    entries: list[dict[str, Any]] = []
    with log_path.open("rb") as stream:
        stream.seek(start)
        if tailing and start:
            stream.readline()  # discard a partial JSON line at the tail boundary
        read_start = stream.tell()
        raw = stream.read(max_bytes)

    complete_length = len(raw)
    if raw and not raw.endswith(b"\n"):
        last_newline = raw.rfind(b"\n")
        complete_length = last_newline + 1 if last_newline >= 0 else 0
    complete = raw[:complete_length]
    next_after = read_start + complete_length

    for raw_line in complete.splitlines():
        try:
            parsed = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict) or not _is_user_visible(parsed):
            continue
        entries.append(
            {
                "id": f"{next_after}-{len(entries)}",
                "occurred_at": str(parsed.get("server_time") or ""),
                "level": str(parsed.get("level") or "INFO").upper(),
                "logger": str(parsed.get("logger") or ""),
                "message": str(parsed.get("message") or ""),
                "path": str(parsed.get("path") or ""),
                "duration_ms": parsed.get("duration_ms"),
                "retry_count": parsed.get("retry_count"),
                "error_type": str(parsed.get("error_type") or ""),
            }
        )

    return {
        "entries": entries[-limit:],
        "next_after": next_after,
        "reset": reset,
    }
