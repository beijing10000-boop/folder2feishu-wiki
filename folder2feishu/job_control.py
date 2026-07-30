from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


class JobStopped(RuntimeError):
    pass


@dataclass(slots=True)
class JobSnapshot:
    run_id: str
    project_id: str
    kind: str
    status: str = "queued"
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    current_item: str = ""
    eta_seconds: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def progress(self) -> float:
        return 0.0 if not self.total else min(1.0, self.completed / self.total)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["progress"] = self.progress
        return value


class JobControl:
    def __init__(self) -> None:
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def checkpoint(self, timeout: float = 0.5) -> None:
        while not self._resume.wait(timeout):
            if self._stop.is_set():
                raise JobStopped("任务已停止")
        if self._stop.is_set():
            raise JobStopped("任务已停止")


Worker = Callable[[JobControl, Callable[..., None]], dict[str, Any] | None]


class BackgroundJobManager:
    """In-process controller; durable state remains in the SQLite ledger."""

    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="folder2feishu",
        )
        self._lock = threading.RLock()
        self._jobs: dict[str, JobSnapshot] = {}
        self._controls: dict[str, JobControl] = {}
        self._futures: dict[str, Future[None]] = {}
        self._active_project: dict[str, str] = {}

    def start(
        self,
        project_id: str,
        kind: str,
        worker: Worker,
        *,
        run_id: str | None = None,
    ) -> JobSnapshot:
        with self._lock:
            active_id = self._active_project.get(project_id)
            if active_id and self._jobs[active_id].status in {
                "queued",
                "running",
                "paused",
            }:
                raise RuntimeError("该项目已有任务正在运行")
            run_id = run_id or uuid.uuid4().hex
            if run_id in self._jobs:
                raise RuntimeError("任务 ID 已存在")
            snapshot = JobSnapshot(run_id=run_id, project_id=project_id, kind=kind)
            control = JobControl()
            self._jobs[run_id] = snapshot
            self._controls[run_id] = control
            self._active_project[project_id] = run_id
            self._futures[run_id] = self._executor.submit(
                self._execute,
                run_id,
                worker,
                control,
            )
            return snapshot

    def _execute(self, run_id: str, worker: Worker, control: JobControl) -> None:
        self._update(run_id, status="running", started_at=utc_now())
        try:
            details = worker(control, lambda **changes: self._update(run_id, **changes))
        except JobStopped:
            self._update(run_id, status="stopped", finished_at=utc_now())
        except Exception as exc:
            self._update(
                run_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=utc_now(),
            )
        else:
            self._update(
                run_id,
                status="done",
                details=details or {},
                finished_at=utc_now(),
            )

    def _update(self, run_id: str, **changes: Any) -> None:
        with self._lock:
            snapshot = self._jobs[run_id]
            for key, value in changes.items():
                if not hasattr(snapshot, key):
                    raise KeyError(f"未知任务字段：{key}")
                setattr(snapshot, key, value)

    def get(self, run_id: str) -> JobSnapshot:
        with self._lock:
            if run_id not in self._jobs:
                raise KeyError(run_id)
            return JobSnapshot(**asdict(self._jobs[run_id]))

    def latest_for_project(self, project_id: str) -> JobSnapshot | None:
        with self._lock:
            run_id = self._active_project.get(project_id)
        return self.get(run_id) if run_id else None

    def pause(self, run_id: str) -> JobSnapshot:
        self._controls[run_id].pause()
        self._update(run_id, status="paused")
        return self.get(run_id)

    def resume(self, run_id: str) -> JobSnapshot:
        with self._lock:
            future = self._futures.get(run_id)
            if future is None or future.done():
                # A quota pause is durable but its in-process worker has
                # already returned. The API must launch a new worker against
                # the same persisted plan instead of changing a dead snapshot.
                raise KeyError(run_id)
            control = self._controls[run_id]
        control.resume()
        self._update(run_id, status="running")
        return self.get(run_id)

    def stop(self, run_id: str) -> JobSnapshot:
        self._controls[run_id].stop()
        return self.get(run_id)

    def close(self) -> None:
        with self._lock:
            for control in self._controls.values():
                control.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
