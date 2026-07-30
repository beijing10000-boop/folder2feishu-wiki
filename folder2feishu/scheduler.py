from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import time
from pathlib import Path

PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    project_id: str
    enabled: bool = False
    local_time: str = "02:00"

    def validate(self) -> None:
        if not PROJECT_ID_PATTERN.fullmatch(self.project_id):
            raise ValueError("项目 ID 格式无效")
        try:
            hour_text, minute_text = self.local_time.split(":", 1)
            time(hour=int(hour_text), minute=int(minute_text))
        except (TypeError, ValueError) as exc:
            raise ValueError("计划时间必须为 HH:MM") from exc


def task_name(project_id: str) -> str:
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise ValueError("项目 ID 格式无效")
    return f"Folder2FeishuWiki-{project_id}"


def application_command(project_id: str) -> list[str]:
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise ValueError("项目 ID 格式无效")
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--run-project", project_id]
    return [
        str(Path(sys.executable).resolve()),
        "-m",
        "folder2feishu",
        "--run-project",
        project_id,
    ]


def _windows_task_command(command: Sequence[str]) -> str:
    # Task Scheduler accepts a single command line.  Quote every component so
    # spaces in Program Files and project IDs cannot be interpreted as syntax.
    return subprocess.list2cmdline(list(command))


def install_daily_schedule(
    spec: ScheduleSpec,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    spec.validate()
    if os.name != "nt":
        raise RuntimeError("Windows 计划任务只可在 Windows 上配置")
    if not spec.enabled:
        remove_schedule(spec.project_id, runner=runner)
        return
    runner(
        [
            "schtasks.exe",
            "/Create",
            "/F",
            "/SC",
            "DAILY",
            "/TN",
            task_name(spec.project_id),
            "/TR",
            _windows_task_command(application_command(spec.project_id)),
            "/ST",
            spec.local_time,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def remove_schedule(
    project_id: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if os.name != "nt":
        return
    completed = runner(
        ["schtasks.exe", "/Delete", "/F", "/TN", task_name(project_id)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError(completed.stderr.strip() or "删除计划任务失败")


def serialize_schedule(spec: ScheduleSpec) -> str:
    spec.validate()
    return json.dumps(asdict(spec), ensure_ascii=False)
