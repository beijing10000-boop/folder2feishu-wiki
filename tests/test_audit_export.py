from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

from folder2feishu.audit_export import export_audit_csv, export_audit_json


def sample_rows():
    return [
        {
            "timestamp": datetime(2026, 7, 30, tzinfo=UTC),
            "level": "INFO",
            "event_type": "UPLOAD_DONE",
            "relative_path": "设计/中文 & emoji 😀.xlsx",
            "status": "done",
            "message": "完成",
        }
    ]


def test_json_export_preserves_unicode() -> None:
    payload = json.loads(export_audit_json(sample_rows()).decode("utf-8"))
    assert payload[0]["relative_path"].endswith("😀.xlsx")


def test_csv_export_is_excel_friendly() -> None:
    raw = export_audit_csv(sample_rows())
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0]["message"] == "完成"


def test_csv_export_neutralizes_formulas() -> None:
    raw = export_audit_csv([{"relative_path": '=HYPERLINK("bad")'}])
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0]["relative_path"].startswith("'=")
