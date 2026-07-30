from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

AUDIT_COLUMNS = (
    "timestamp",
    "level",
    "event_type",
    "project_id",
    "run_id",
    "relative_path",
    "action",
    "status",
    "message",
    "local_sha256",
    "drive_file_token",
    "wiki_node_token",
)


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if value is None:
        return ""
    if isinstance(value, str | int | float | bool):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_audit_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [{column: _plain(row.get(column)) for column in AUDIT_COLUMNS} for row in rows]


def export_audit_json(rows: Iterable[Mapping[str, Any]]) -> bytes:
    payload = normalize_audit_rows(rows)
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def export_audit_csv(rows: Iterable[Mapping[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=AUDIT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(
        {
            key: (
                f"'{value}"
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                else value
            )
            for key, value in row.items()
        }
        for row in normalize_audit_rows(rows)
    )
    # UTF-8 BOM keeps Chinese paths readable when opened directly in Excel.
    return ("\ufeff" + stream.getvalue()).encode("utf-8")
