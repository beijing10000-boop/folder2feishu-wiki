from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from folder2feishu.api import create_app
from folder2feishu.application import ApplicationServices
from folder2feishu.core import ActionType, MigrationState, RunStatus, RunType, UploadStatus
from folder2feishu.core.models import utc_now
from folder2feishu.job_control import JobSnapshot
from folder2feishu.runtime import RuntimePaths
from folder2feishu.security import MemoryCredentialStore
from folder2feishu.verification import VerificationResult


def test_local_api_contract_and_wrapper_fields(tmp_path: Path) -> None:
    source = tmp_path / "OneDrive source"
    source.mkdir()
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    app = create_app(services, static_root=Path("frontend/dist").resolve())
    try:
        with TestClient(app) as client:
            assert client.get("/api/v2/health").json()["status"] == "ok"
            token = client.get("/api/v2/session").json()["csrf_token"]
            headers = {"X-F2F-CSRF": token}
            rejected = client.post(
                "/api/v2/projects",
                json={
                    "name": "pilot",
                    "source_root": str(source),
                    "target_wiki_url": "https://example.feishu.cn/wiki/ABCDEFGHIJKL",
                },
            )
            assert rejected.status_code == 403
            assert rejected.json()["error"]["code"] == "LOCAL_REQUEST_REJECTED"

            created = client.post(
                "/api/v2/projects",
                headers=headers,
                json={
                    "name": "pilot",
                    "source_root": str(source),
                    "target_wiki_url": "https://example.feishu.cn/wiki/ABCDEFGHIJKL",
                    "create_wrapper": True,
                    "wrapper_name": "Folder2Feishu_Pilot_20260730",
                },
            )
            assert created.status_code == 200, created.text
            project = created.json()
            assert project["wrapper_name"] == "Folder2Feishu_Pilot_20260730"
            assert project["last_run_id"] is None

            before_scan = client.get(
                f"/api/v2/projects/{project['id']}/preflight",
            )
            assert before_scan.status_code == 200, before_scan.text
            assert before_scan.json()["complete"] is False
            assert before_scan.json()["writable"] is False
            assert any(
                check["code"] == "scan_complete" and check["blocking"]
                for check in before_scan.json()["checks"]
            )

            updated = client.patch(
                f"/api/v2/projects/{project['id']}",
                headers=headers,
                json={
                    "name": "pilot-updated",
                    "create_wrapper": True,
                    "wrapper_name": "Pilot root",
                },
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["wrapper_name"] == "Pilot root"

            invalid = client.post(
                "/api/v2/projects",
                headers=headers,
                json={"name": ""},
            )
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        services.close()


def test_run_payload_exposes_multipart_progress_and_runtime_log_tail(tmp_path: Path) -> None:
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    project = services.store.create_project(name="live", source_root=tmp_path / "source")
    action = services.store.save_plan(
        project.id,
        "plan-live",
        [
            {
                "action_type": ActionType.UPLOAD,
                "state": MigrationState.UPLOADING,
                "source_rel_path": "Training/video.mp4",
            }
        ],
    )[0]
    services.store.upsert_upload_session(
        project_id=project.id,
        planned_action_id=action.id,
        upload_id="upload-live",
        idempotency_key="video",
        file_size=12,
        part_size=4,
        total_parts=3,
    )
    services.store.record_upload_progress(
        action.id,
        completed_part=0,
        status=UploadStatus.UPLOADING,
    )
    run = services.store.create_job_run(
        project.id,
        RunType.MIGRATION,
        status=RunStatus.RUNNING,
        plan_id="plan-live",
    )
    services.store.update_job_run(run.id, heartbeat_at=utc_now())
    (services.paths.logs / "folder2feishu.log").write_text(
        '{"server_time":"2026-08-04T02:31:12+00:00","level":"INFO",'
        '"logger":"httpx","message":"HTTP Request: POST '
        "https://open.feishu.cn/open-apis/drive/v1/files/upload_part "
        '\\"HTTP/1.1 200 OK\\""}\n',
        encoding="utf-8",
    )
    app = create_app(services, static_root=Path("frontend/dist").resolve())
    try:
        with TestClient(app) as client:
            payload = client.get(f"/api/v2/runs/{run.id}").json()
            assert payload["active_uploads"][0] == {
                "action_id": action.id,
                "relative_path": "Training/video.mp4",
                "status": "UPLOADING",
                "completed_parts": 1,
                "total_parts": 3,
                "uploaded_bytes": 4,
                "total_bytes": 12,
                "percent": 33.3,
                "attempts": 0,
                "last_error": "",
                "updated_at": payload["active_uploads"][0]["updated_at"],
            }
            logs = client.get("/api/v2/runtime/logs").json()
            assert len(logs["entries"]) == 1
            assert "upload_part" in logs["entries"][0]["message"]
    finally:
        services.close()


def test_duplicate_scan_is_an_expected_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    project = services.store.create_project(
        name="active-scan",
        source_root=str(source),
        target_wiki_url="https://example.feishu.cn/wiki/ABCDEFGHIJKL",
    )

    def reject_duplicate(*args, **kwargs):
        raise RuntimeError("该项目已有任务正在运行")

    monkeypatch.setattr(services.jobs, "start", reject_duplicate)
    app = create_app(services, static_root=Path("frontend/dist").resolve())
    try:
        with TestClient(app) as client:
            csrf = client.get("/api/v2/session").json()["csrf_token"]
            response = client.post(
                f"/api/v2/projects/{project.id}/scan",
                headers={"X-F2F-CSRF": csrf},
                json={},
            )
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "JOB_ALREADY_RUNNING"
            assert "仍在运行" in response.json()["error"]["message"]
    finally:
        services.close()


def test_scan_status_does_not_materialize_the_complete_inventory_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    project = services.store.create_project(
        name="large-inventory",
        source_root=str(source),
        target_wiki_url="https://example.feishu.cn/wiki/ABCDEFGHIJKL",
    )
    summary_calls = 0

    def summary(_: str) -> dict:
        nonlocal summary_calls
        summary_calls += 1
        return {
            "files": 51_527,
            "folders": 6_536,
            "bytes": 307_000_000_000,
            "scan_complete": True,
        }

    monkeypatch.setattr(services, "inventory_summary", summary)
    monkeypatch.setattr(
        services,
        "inventory_tree",
        lambda _: (_ for _ in ()).throw(AssertionError("scan status loaded the tree")),
    )
    app = create_app(services, static_root=Path("frontend/dist").resolve())
    try:
        with TestClient(app) as client:
            response = client.get(f"/api/v2/projects/{project.id}/scan")
    finally:
        services.close()

    assert response.status_code == 200, response.text
    assert response.json()["summary"]["files"] == 51_527
    assert response.json()["tree"] == []
    assert summary_calls == 1


def test_plan_submission_returns_a_durable_task_and_deduplicates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    project = services.store.create_project(
        name="background-plan",
        source_root=str(source),
        target_wiki_url="https://example.feishu.cn/wiki/ABCDEFGHIJKL",
    )
    starts = 0

    def fake_start(project_id, kind, worker, *, run_id=None):
        nonlocal starts
        starts += 1
        return JobSnapshot(run_id=run_id, project_id=project_id, kind=kind)

    monkeypatch.setattr(services.jobs, "start", fake_start)
    app = create_app(services, static_root=Path("frontend/dist").resolve())
    try:
        with TestClient(app) as client:
            csrf = client.get("/api/v2/session").json()["csrf_token"]
            headers = {"X-F2F-CSRF": csrf}
            first = client.post(
                f"/api/v2/projects/{project.id}/plan",
                headers=headers,
                json={"confirmed": False},
            )
            second = client.post(
                f"/api/v2/projects/{project.id}/plan",
                headers=headers,
                json={"confirmed": False},
            )
            tasks = client.get(f"/api/v2/projects/{project.id}/tasks")
    finally:
        services.close()

    assert first.status_code == 200, first.text
    assert first.json()["kind"] == "PLAN"
    assert first.json()["id"]
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["deduplicated"] is True
    assert starts == 1
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()[0]["id"] == first.json()["id"]


def test_retry_and_restart_resume_keep_the_original_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    project = services.store.create_project(
        name="durable",
        source_root=str(source),
        target_wiki_url="https://example.feishu.cn/wiki/ABCDEFGHIJKL",
    )
    project = services.store.update_project(
        project.id,
        current_scan_id="scan-A",
        scan_complete=True,
    )
    services.store.save_plan(
        project.id,
        "original-plan",
        [
            {
                "action_type": ActionType.SKIP,
                "details": {"plan_confirmed": True},
            }
        ],
    )
    previous = services.store.create_job_run(
        project.id,
        RunType.MIGRATION,
        status=RunStatus.PAUSED,
        scan_id="scan-A",
        plan_id="original-plan",
    )
    services.store.save_plan(
        project.id,
        "newer-plan",
        [
            {
                "action_type": ActionType.SKIP,
                "details": {"plan_confirmed": True},
            }
        ],
    )

    started_plan_ids: list[str | None] = []

    def fake_start(project_id, kind, worker, *, run_id=None):
        durable = services.store.get_job_run(run_id)
        started_plan_ids.append(durable.plan_id)
        return JobSnapshot(
            run_id=run_id,
            project_id=project_id,
            kind=kind,
        )

    monkeypatch.setattr(services.jobs, "start", fake_start)
    monkeypatch.setattr(
        services.jobs,
        "resume",
        lambda run_id: (_ for _ in ()).throw(KeyError(run_id)),
    )
    app = create_app(services, static_root=Path("frontend/dist").resolve())
    try:
        with TestClient(app) as client:
            csrf = client.get("/api/v2/session").json()["csrf_token"]
            headers = {"X-F2F-CSRF": csrf}
            retried = client.post(
                f"/api/v2/runs/{previous.id}/retry",
                headers=headers,
                json={},
            )
            assert retried.status_code == 200, retried.text
            resumed = client.post(
                f"/api/v2/runs/{previous.id}/resume",
                headers=headers,
                json={},
            )
            assert resumed.status_code == 200, resumed.text
    finally:
        services.close()

    assert started_plan_ids == ["original-plan", "original-plan"]


def test_configuration_verification_endpoints_are_csrf_protected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    monkeypatch.setattr(
        services,
        "verify_app_configuration",
        lambda: VerificationResult("app", "app verified"),
    )
    monkeypatch.setattr(
        services,
        "verify_oauth_configuration",
        lambda: VerificationResult(
            "oauth",
            "oauth verified",
            {"user_name": "迁移管理员", "scope_count": 6},
        ),
    )
    monkeypatch.setattr(
        services,
        "verify_source_configuration",
        lambda source_root: VerificationResult(
            "source",
            "source verified",
            {"normalized_path": source_root},
        ),
    )
    monkeypatch.setattr(
        services,
        "verify_target_configuration",
        lambda target_wiki_url: VerificationResult(
            "target",
            "target verified",
            {
                "space_id": "space",
                "node_token": target_wiki_url.rsplit("/", 1)[-1],
                "page_editable": True,
                "container_edit_requires_pilot": True,
            },
        ),
    )
    app = create_app(services, static_root=Path("frontend/dist").resolve())
    try:
        with TestClient(app) as client:
            rejected = client.post("/api/v2/verify/app", json={})
            assert rejected.status_code == 403

            csrf = client.get("/api/v2/session").json()["csrf_token"]
            headers = {"X-F2F-CSRF": csrf}
            cases = [
                ("/api/v2/verify/app", {}, "app"),
                ("/api/v2/verify/oauth", {}, "oauth"),
                (
                    "/api/v2/verify/source",
                    {"source_root": r"D:\OneDrive"},
                    "source",
                ),
                (
                    "/api/v2/verify/target",
                    {"target_wiki_url": ("https://example.feishu.cn/wiki/WikiParentToken99")},
                    "target",
                ),
            ]
            for path, body, kind in cases:
                response = client.post(path, headers=headers, json=body)
                assert response.status_code == 200, response.text
                assert response.json()["ok"] is True
                assert response.json()["kind"] == kind
    finally:
        services.close()
