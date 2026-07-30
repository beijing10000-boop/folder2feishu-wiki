from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from folder2feishu.cli import run_project
from folder2feishu.core import ActionType, CoreStore, MigrationState, RunStatus, RunType
from folder2feishu.executor import ExecutionResult


class _MustNotRun:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"restart recovery must not call {name}")


def test_headless_restart_resumes_bound_plan_without_rescan_or_replan(
    tmp_path: Path,
) -> None:
    store = CoreStore(tmp_path / "ledger.db")
    try:
        source = tmp_path / "source"
        source.mkdir()
        project = store.create_project(name="resume", source_root=source)
        store.update_project(project.id, current_scan_id="scan-1")
        action = store.save_plan(
            project.id,
            "durable-plan",
            [
                {
                    "action_type": ActionType.UPLOAD,
                    "state": MigrationState.RETRYABLE,
                    "source_rel_path": "report.xlsx",
                    "destination_rel_path": "report.xlsx",
                    "drive_file_token": "persisted-file-token",
                    "move_task_id": "persisted-task-id",
                    "details": {"plan_confirmed": True},
                }
            ],
        )[0]
        run = store.create_job_run(
            project.id,
            RunType.MIGRATION,
            status=RunStatus.FAILED,
            scan_id="scan-1",
            plan_id=action.plan_id,
        )
        captured: dict[str, str | None] = {}

        class _Executor:
            def execute(
                self,
                project_id: str,
                *,
                run_id: str | None = None,
            ) -> ExecutionResult:
                captured["project_id"] = project_id
                captured["run_id"] = run_id
                return ExecutionResult(
                    run_id=run_id or "",
                    total=1,
                    completed=1,
                    skipped=0,
                    failed=0,
                    conflicts=0,
                )

        services = SimpleNamespace(
            store=store,
            scanner=_MustNotRun(),
            planner=_MustNotRun(),
            preflight=lambda _: SimpleNamespace(ready=True, checks=[]),
            executor=lambda: _Executor(),
        )

        assert run_project(services, project.id) == 0  # type: ignore[arg-type]
        assert captured == {"project_id": project.id, "run_id": run.id}
        actions = store.list_plan_actions(project.id)
        assert len(actions) == 1
        assert actions[0].drive_file_token == "persisted-file-token"
        assert actions[0].move_task_id == "persisted-task-id"
    finally:
        store.close()
