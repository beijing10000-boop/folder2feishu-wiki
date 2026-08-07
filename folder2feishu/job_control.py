from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .observability import stage_var, task_id_var

LOGGER = logging.getLogger(__name__)


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

    def wait(self, seconds: float, *, interval: float = 0.25) -> None:
        """Interruptible replacement for time.sleep used by retries and polling.

        The wait must actually elapse: it backs the retry/backoff path in
        ``FeishuAPIClient``, so collapsing it to a no-op would turn a single
        429 into a burst of instant retries against Feishu.
        """

        remaining = max(0.0, float(seconds))
        step = max(0.01, float(interval))
        while remaining > 0.0:
            # A pause holds here without consuming the backoff budget, and
            # raises JobStopped as soon as the operator stops the task.
            self.checkpoint(timeout=step)
            slice_seconds = min(step, remaining)
            # Event.wait blocks for the slice but returns immediately once a
            # stop is requested, so the sleep stays interruptible.
            if self._stop.wait(slice_seconds):
                raise JobStopped("任务已停止")
            remaining -= slice_seconds


class HeartbeatPump:
    """Persist a task heartbeat even while its worker is inside a network call."""

    def __init__(self, callback: Callable[[], Any], *, interval_seconds: float = 5.0) -> None:
        self._callback = callback
        self._interval = max(1.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> HeartbeatPump:
        self._callback()
        self._thread = threading.Thread(
            target=self._run,
            name="folder2feishu-task-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._callback()
            except Exception:
                # The main worker remains authoritative. Its next durable update
                # will surface a database failure without killing this process.
                return


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
                if self._jobs[run_id].status in {"done", "failed", "stopped"}:
                    self._jobs.pop(run_id, None)
                    self._controls.pop(run_id, None)
                    self._futures.pop(run_id, None)
                else:
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
        task_token = task_id_var.set(run_id)
        stage_token = stage_var.set(self._jobs[run_id].kind.upper())
        self._update(run_id, status="running", started_at=utc_now())
        LOGGER.info("后台任务开始")
        try:
            details = worker(control, lambda **changes: self._update(run_id, **changes))
        except JobStopped:
            LOGGER.info("后台任务已停止")
            self._update(run_id, status="stopped", finished_at=utc_now())
        except Exception as exc:
            LOGGER.exception(
                "后台任务失败",
                extra={"error_type": type(exc).__name__},
            )
            self._update(
                run_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=utc_now(),
            )
        else:
            LOGGER.info("后台任务完成")
            self._update(
                run_id,
                status="done",
                details=details or {},
                finished_at=utc_now(),
            )
        finally:
            stage_var.reset(stage_token)
            task_id_var.reset(task_token)

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
