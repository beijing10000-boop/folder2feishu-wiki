import json

from folder2feishu.runtime_logs import read_runtime_logs


def _line(**changes):
    payload = {
        "server_time": "2026-08-04T02:31:12+00:00",
        "level": "INFO",
        "logger": "httpx",
        "message": (
            "HTTP Request: POST https://open.feishu.cn/open-apis/drive/v1/files/"
            'upload_part "HTTP/1.1 200 OK"'
        ),
        "request_id": "",
        "task_id": "",
        "stage": "",
    }
    payload.update(changes)
    return json.dumps(payload, ensure_ascii=False) + "\n"


def test_runtime_log_tail_filters_polling_and_reads_incrementally(tmp_path):
    path = tmp_path / "folder2feishu.log"
    path.write_text(
        _line(logger="folder2feishu.api", message="HTTP request completed") + _line(),
        encoding="utf-8",
    )

    first = read_runtime_logs(path)
    assert len(first["entries"]) == 1
    assert "upload_part" in first["entries"][0]["message"]

    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            _line(
                level="WARNING",
                logger="folder2feishu.feishu.client",
                message="Feishu rate limit reached; endpoint bucket deferred",
                path="/drive/v1/files/upload_part",
                retry_count=2,
            )
        )

    second = read_runtime_logs(path, after=first["next_after"])
    assert len(second["entries"]) == 1
    assert second["entries"][0]["retry_count"] == 2
    assert second["next_after"] > first["next_after"]


def test_runtime_log_cursor_resets_after_rotation(tmp_path):
    path = tmp_path / "folder2feishu.log"
    path.write_text(_line(), encoding="utf-8")

    result = read_runtime_logs(path, after=path.stat().st_size + 100)

    assert result["reset"] is True
    assert len(result["entries"]) == 1
