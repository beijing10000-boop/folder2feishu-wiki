from __future__ import annotations

import threading
import time

import pytest

from folder2feishu.job_control import BackgroundJobManager


def wait_terminal(manager: BackgroundJobManager, run_id: str) -> str:
    for _ in range(100):
        status = manager.get(run_id).status
        if status in {"done", "failed", "stopped"}:
            return status
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_job_progress_and_single_project_lease() -> None:
    manager = BackgroundJobManager(max_workers=1)
    release = threading.Event()

    def worker(control, update):
        update(total=2, current_item="a")
        control.checkpoint()
        release.wait(1)
        update(completed=1, current_item="b")
        control.checkpoint()
        update(completed=2)
        return {"ok": True}

    job = manager.start("p1", "scan", worker)
    with pytest.raises(RuntimeError, match="正在运行"):
        manager.start("p1", "scan", worker)
    release.set()
    assert wait_terminal(manager, job.run_id) == "done"
    snapshot = manager.get(job.run_id)
    assert snapshot.progress == 1
    assert snapshot.details == {"ok": True}
    manager.close()


def test_job_can_be_stopped() -> None:
    manager = BackgroundJobManager(max_workers=1)

    def worker(control, update):
        update(total=100)
        for index in range(100):
            time.sleep(0.005)
            control.checkpoint()
            update(completed=index + 1)
        return None

    job = manager.start("p2", "migration", worker)
    manager.stop(job.run_id)
    assert wait_terminal(manager, job.run_id) == "stopped"
    manager.close()


def test_finished_worker_cannot_be_fake_resumed() -> None:
    manager = BackgroundJobManager(max_workers=1)
    job = manager.start("p3", "migration", lambda control, update: {"quota_paused": True})
    assert wait_terminal(manager, job.run_id) == "done"

    with pytest.raises(KeyError):
        manager.resume(job.run_id)

    manager.close()
