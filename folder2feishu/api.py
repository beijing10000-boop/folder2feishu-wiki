"""Localhost-only FastAPI application for the Windows operations console."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .api_models import (
    ProjectCreate,
    ProjectUpdate,
    SettingsUpdate,
    SourceVerifyRequest,
    TargetVerifyRequest,
    VerificationResponse,
    VerifyCommand,
)
from .application import ApplicationServices
from .audit_export import export_audit_csv, export_audit_json
from .core import (
    AuditLevel,
    MigrationState,
    PlanBlockedError,
    ProjectStatus,
    RunType,
)
from .core import (
    RunStatus as LedgerRunStatus,
)
from .core.models import utc_now
from .feishu import FeishuError
from .job_control import JobSnapshot
from .quota import DailyQuotaStore
from .runtime import bundled_path
from .settings import PublicSettings
from .web_security import LocalRequestGuard, new_csrf_token

LOGGER = logging.getLogger(__name__)


class ApiFailure(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class EmptyCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False


def _error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"code": code, "message": message}
    if details:
        payload["details"] = details
    return JSONResponse(status_code=status_code, content={"error": payload})


def _project_payload(services: ApplicationServices, project: Any) -> dict[str, Any]:
    runs = services.store.list_job_runs(project.id, limit=20)
    last_migration = next((run for run in runs if run.run_type == RunType.MIGRATION), None)
    return {
        "id": project.id,
        "name": project.name,
        "source_root": project.source_root,
        "target_wiki_url": project.target_wiki_url,
        "target_space_id": project.target_space_id,
        "target_parent_token": project.target_parent_node_token,
        "target_parent_node_token": project.target_parent_node_token,
        "wrapper_name": project.wrapper_name,
        "incremental_mode": "safe",
        "mode": project.incremental_policy,
        "last_run_id": last_migration.id if last_migration else None,
        "status": project.status.value,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def _issue_payload(issue: Any) -> dict[str, Any]:
    return {
        "code": issue.code.value,
        "severity": (
            "error"
            if issue.severity.value == "BLOCKING"
            else "warning"
            if issue.severity.value == "WARNING"
            else "info"
        ),
        "relative_path": issue.rel_path,
        "rel_path": issue.rel_path,
        "title": issue.code.value,
        "message": issue.message,
        "blocking": issue.severity.value == "BLOCKING",
    }


def _scan_payload(services: ApplicationServices, project_id: str) -> dict[str, Any]:
    project = services.store.get_project(project_id)
    latest = services.jobs.latest_for_project(project_id)
    issues = services.store.list_issues(project_id, scan_id=project.current_scan_id)
    status = (
        latest.status.upper()
        if latest and latest.kind == "scan"
        else "COMPLETED"
        if project.scan_complete
        else "IDLE"
    )
    return {
        "scan_id": project.current_scan_id or "",
        "run_id": latest.run_id if latest and latest.kind == "scan" else None,
        "status": status,
        "complete": project.scan_complete,
        "cancel_requested": bool(latest and latest.kind == "scan" and latest.status == "stopped"),
        "summary": services.inventory_summary(project_id),
        "counts": services.inventory_summary(project_id),
        "checks": [_issue_payload(issue) for issue in issues],
        "issues": [_issue_payload(issue) for issue in issues],
        "tree": services.inventory_tree(project_id),
        "scanned_items": (int(latest.details.get("scanned_items", 0)) if latest else 0),
        "current_path": latest.current_item if latest else "",
        "started_at": latest.started_at.isoformat() if latest and latest.started_at else None,
        "finished_at": latest.finished_at.isoformat() if latest and latest.finished_at else None,
    }


def _job_payload(
    services: ApplicationServices,
    run_id: str,
    snapshot: JobSnapshot | None = None,
) -> dict[str, Any]:
    run = services.store.get_job_run(run_id)
    project = services.store.get_project(run.project_id)
    actions = services.store.list_plan_actions(project.id, plan_id=run.plan_id)
    quota = DailyQuotaStore(
        services.paths.quota,
        budget=services.settings_store.load().daily_upload_budget,
    ).snapshot()
    status_map = {
        LedgerRunStatus.QUEUED: "IDLE",
        LedgerRunStatus.RUNNING: "RUNNING",
        LedgerRunStatus.PAUSED: "PAUSED",
        LedgerRunStatus.COMPLETE: "COMPLETED",
        LedgerRunStatus.FAILED: "FAILED",
        LedgerRunStatus.CANCELLED: "STOPPED",
    }
    state = status_map[run.status]
    if snapshot and snapshot.status in {"running", "paused", "stopped", "failed"}:
        state = {
            "running": "RUNNING",
            "paused": "PAUSED",
            "stopped": "STOPPED",
            "failed": "FAILED",
        }[snapshot.status]
    bytes_total = sum(int((action.details or {}).get("source_size") or 0) for action in actions)
    return {
        "id": run.id,
        "run_id": run.id,
        "project_id": run.project_id,
        "state": state,
        "status": state,
        "total": run.total_items or len(actions),
        "completed": run.completed_items,
        "failed": run.failed_items,
        "skipped": run.skipped_items,
        "conflicts": int((run.summary or {}).get("conflicts", 0)),
        "current_path": snapshot.current_item if snapshot else "",
        "progress": {
            "total": run.total_items or len(actions),
            "completed": run.completed_items,
            "failed": run.failed_items,
        },
        "bytes_total": bytes_total,
        "bytes_completed": 0,
        "eta_seconds": snapshot.eta_seconds if snapshot else None,
        "quota": {
            "upload_calls_used": quota.used,
            "upload_calls_limit": quota.budget,
            "used": quota.used,
            "budget": quota.budget,
            "wiki_calls_minute": 0,
            "wiki_calls_limit": services.settings_store.load().wiki_calls_per_minute,
            "next_reset_at": quota.reset_at.isoformat(),
            "reset_at": quota.reset_at.isoformat(),
        },
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
    }


def _audit_rows(services: ApplicationServices, project_id: str) -> list[dict[str, Any]]:
    actions = {action.id: action for action in services.store.list_plan_actions(project_id)}
    rows: list[dict[str, Any]] = []
    for event in services.store.list_audit(project_id):
        action = actions.get(event.planned_action_id) if event.planned_action_id else None
        payload = event.payload or {}
        rows.append(
            {
                "timestamp": event.created_at,
                "level": event.level.value,
                "event_type": event.event_type,
                "project_id": event.project_id,
                "run_id": event.job_run_id,
                "relative_path": event.rel_path,
                "action": action.action_type.value if action else "",
                "status": action.state.value if action else "",
                "message": event.message,
                "local_sha256": payload.get("local_sha256", ""),
                "drive_file_token": payload.get("drive_file_token", ""),
                "wiki_node_token": payload.get("wiki_node_token", ""),
            }
        )
    return rows


def create_app(
    services: ApplicationServices | None = None,
    *,
    static_root: Path | None = None,
) -> FastAPI:
    owned_services = services is None
    services = services or ApplicationServices()
    csrf_token = new_csrf_token()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if owned_services:
            services.close()

    app = FastAPI(
        title="Folder2Feishu Wiki",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.csrf_token = csrf_token
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    app.add_middleware(LocalRequestGuard, csrf_token=csrf_token)

    @app.exception_handler(ApiFailure)
    async def handle_api_failure(_: Request, exc: ApiFailure) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(KeyError)
    async def handle_not_found(_: Request, exc: KeyError) -> JSONResponse:
        LOGGER.info("requested object was not found: %s", exc)
        return _error(404, "NOT_FOUND", "请求的项目或任务不存在")

    @app.exception_handler(ValueError)
    async def handle_invalid(_: Request, exc: ValueError) -> JSONResponse:
        return _error(422, "INVALID_INPUT", str(exc))

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = {
            "fields": [
                {
                    "location": ".".join(str(part) for part in error["loc"]),
                    "message": error["msg"],
                    "type": error["type"],
                }
                for error in exc.errors()
            ]
        }
        return _error(422, "VALIDATION_ERROR", "请求参数无效", details)

    @app.exception_handler(FeishuError)
    async def handle_feishu(_: Request, exc: Exception) -> JSONResponse:
        return _error(502, "FEISHU_REQUEST_FAILED", str(exc))

    @app.exception_handler(PlanBlockedError)
    async def handle_plan_blocked(_: Request, exc: PlanBlockedError) -> JSONResponse:
        LOGGER.info("request is waiting for a complete scan: %s", exc)
        return _error(409, "SCAN_REQUIRED", "请先等待本地目录盘点完整结束")

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("unexpected request failure", exc_info=exc)
        return _error(500, "INTERNAL_ERROR", "本机服务发生未预期错误，请查看日志")

    @app.get("/api/v2/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "status": "ok", "version": __version__, "database": "ok"}

    @app.get("/api/v2/session")
    def session() -> dict[str, str]:
        return {"csrf_token": csrf_token}

    @app.get("/api/v2/settings")
    def get_settings() -> dict[str, Any]:
        return services.public_settings()

    @app.put("/api/v2/settings")
    def put_settings(value: SettingsUpdate) -> dict[str, Any]:
        current = services.settings_store.load()
        settings = PublicSettings(
            app_id=value.app_id.strip(),
            redirect_uri=value.redirect_uri.strip(),
            scopes=value.scopes,
            host=current.host,
            port=current.port,
            upload_qps=value.upload_qps,
            wiki_calls_per_minute=value.wiki_calls_per_minute,
            daily_upload_budget=value.daily_upload_budget,
            open_browser=current.open_browser,
        )
        return services.save_settings(settings, app_secret=value.app_secret)

    @app.get("/api/v2/auth/status")
    def auth_status() -> dict[str, Any]:
        return services.auth_status()

    @app.post("/api/v2/auth/start")
    def auth_start(_: EmptyCommand) -> dict[str, str]:
        return {"authorization_url": services.begin_authorization()}

    @app.post("/api/v2/verify/app", response_model=VerificationResponse)
    def verify_app(_: VerifyCommand) -> dict[str, Any]:
        return services.verify_app_configuration().as_dict()

    @app.post("/api/v2/verify/oauth", response_model=VerificationResponse)
    def verify_oauth(_: VerifyCommand) -> dict[str, Any]:
        return services.verify_oauth_configuration().as_dict()

    @app.post("/api/v2/verify/source", response_model=VerificationResponse)
    def verify_source(value: SourceVerifyRequest) -> dict[str, Any]:
        return services.verify_source_configuration(value.source_root).as_dict()

    @app.post("/api/v2/verify/target", response_model=VerificationResponse)
    def verify_target(value: TargetVerifyRequest) -> dict[str, Any]:
        return services.verify_target_configuration(value.target_wiki_url).as_dict()

    @app.get("/oauth/callback")
    def oauth_callback(code: str = "", state: str = "", error: str = "") -> Response:
        if error:
            raise ApiFailure(400, "OAUTH_REJECTED", "飞书授权未完成")
        services.complete_authorization(code=code, state=state)
        return RedirectResponse(url="/?oauth=success", status_code=303)

    @app.get("/api/v2/projects")
    def list_projects() -> list[dict[str, Any]]:
        return [_project_payload(services, project) for project in services.store.list_projects()]

    @app.post("/api/v2/projects")
    def create_project(value: ProjectCreate) -> dict[str, Any]:
        if not value.create_wrapper:
            raise ValueError("知识库迁移必须创建同名根包装节点")
        project = services.create_project(
            name=value.name,
            source_root=value.source_root,
            target_wiki_url=value.target_wiki_url,
            wrapper_name=value.wrapper_name,
        )
        return _project_payload(services, project)

    @app.get("/api/v2/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return _project_payload(services, services.store.get_project(project_id))

    @app.patch("/api/v2/projects/{project_id}")
    def update_project(project_id: str, value: ProjectUpdate) -> dict[str, Any]:
        updates = value.model_dump(exclude_none=True)
        create_wrapper = updates.pop("create_wrapper", None)
        if create_wrapper is False:
            raise ValueError("知识库迁移必须保留同名根包装节点")
        if "source_root" in updates:
            assert isinstance(updates["source_root"], str)
            from .runtime import assert_runtime_outside_source

            assert_runtime_outside_source(services.paths, updates["source_root"])
        if updates:
            project = services.store.update_project(project_id, **updates)
        else:
            project = services.store.get_project(project_id)
        return _project_payload(services, project)

    @app.post("/api/v2/projects/{project_id}/scan")
    def start_scan(project_id: str, _: EmptyCommand) -> dict[str, Any]:
        services.store.get_project(project_id)

        def worker(control: Any, update: Any) -> dict[str, Any]:
            result = services.scanner.scan(
                project_id,
                cancel=lambda: control.stop_requested,
                progress=lambda current: update(
                    completed=current.scanned_items,
                    current_item=current.current_rel_path,
                    details={
                        "scanned_items": current.scanned_items,
                        "folders": current.folders,
                        "files": current.files,
                        "bytes": current.bytes,
                    },
                ),
            )
            return asdict(result)

        try:
            job = services.jobs.start(project_id, "scan", worker)
        except RuntimeError as exc:
            if "已有任务正在运行" not in str(exc):
                raise
            raise ApiFailure(
                409,
                "JOB_ALREADY_RUNNING",
                "当前项目的盘点任务仍在运行，请勿重复启动",
            ) from exc
        return {"run_id": job.run_id, "status": job.status}

    @app.get("/api/v2/projects/{project_id}/scan")
    def get_scan(project_id: str) -> dict[str, Any]:
        return _scan_payload(services, project_id)

    @app.get("/api/v2/projects/{project_id}/preflight")
    def get_preflight(project_id: str) -> dict[str, Any]:
        report = services.preflight(project_id)
        summary = services.inventory_summary(project_id)
        return {
            **asdict(report),
            "complete": report.ready,
            "counts": summary,
            "estimated_upload_calls": summary["upload_calls"],
            "estimated_days": summary["estimated_days"],
        }

    @app.get("/api/v2/projects/{project_id}/tree")
    def get_tree(project_id: str) -> list[dict[str, Any]]:
        return services.inventory_tree(project_id)

    @app.post("/api/v2/projects/{project_id}/plan")
    def post_plan(project_id: str, command: PlanCommand) -> dict[str, Any]:
        if command.confirmed:
            return services.confirm_latest_plan(project_id)
        preflight = services.preflight(project_id)
        if not preflight.ready:
            raise ApiFailure(
                409,
                "PREFLIGHT_BLOCKED",
                "预检仍有阻断项，不能生成写入计划",
                {"checks": preflight.checks},
            )
        services.planner.build(project_id)
        return services.plan_payload(project_id)

    @app.get("/api/v2/projects/{project_id}/plan")
    def get_plan(project_id: str) -> dict[str, Any]:
        return services.plan_payload(project_id)

    def launch_migration(
        project_id: str,
        *,
        plan_id: str | None = None,
        scan_id: str | None = None,
    ) -> dict[str, Any]:
        project = services.store.get_project(project_id)
        if scan_id and project.current_scan_id != scan_id:
            raise ApiFailure(
                409,
                "RUN_SCAN_CHANGED",
                "该运行绑定的本地盘点已被新盘点替换，不能继续旧计划；请确认新差异计划",
            )
        actions = services.store.list_plan_actions(project_id, plan_id=plan_id)
        if not actions:
            raise ApiFailure(409, "PLAN_REQUIRED", "请先生成并确认差异计划")
        selected_plan_id = actions[0].plan_id
        if any(action.plan_id != selected_plan_id for action in actions):
            raise ApiFailure(409, "PLAN_INCONSISTENT", "迁移计划状态不一致，请重新生成计划")
        durable = services.store.create_job_run(
            project_id,
            RunType.MIGRATION,
            status=LedgerRunStatus.QUEUED,
            scan_id=scan_id or project.current_scan_id,
            plan_id=selected_plan_id,
        )

        def worker(control: Any, update: Any) -> dict[str, Any]:
            result = services.executor().execute(
                project_id,
                run_id=durable.id,
                control=control,
                progress=update,
            )
            return asdict(result)

        try:
            job = services.jobs.start(
                project_id,
                "migration",
                worker,
                run_id=durable.id,
            )
        except Exception:
            services.store.update_job_run(
                durable.id,
                status=LedgerRunStatus.FAILED,
                error="无法启动本机后台任务",
                finished_at=utc_now(),
            )
            raise
        return _job_payload(services, job.run_id, job)

    @app.post("/api/v2/projects/{project_id}/runs")
    def start_run(project_id: str, _: EmptyCommand) -> dict[str, Any]:
        return launch_migration(project_id)

    @app.get("/api/v2/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            snapshot = services.jobs.get(run_id)
        except KeyError:
            snapshot = None
        return _job_payload(services, run_id, snapshot)

    @app.post("/api/v2/runs/{run_id}/pause")
    def pause_run(run_id: str, _: EmptyCommand) -> dict[str, Any]:
        snapshot = services.jobs.pause(run_id)
        services.store.update_job_run(run_id, status=LedgerRunStatus.PAUSED)
        services.store.update_project(snapshot.project_id, status=ProjectStatus.PAUSED)
        return _job_payload(services, run_id, snapshot)

    @app.post("/api/v2/runs/{run_id}/resume")
    def resume_run(run_id: str, _: EmptyCommand) -> dict[str, Any]:
        try:
            snapshot = services.jobs.resume(run_id)
        except KeyError:
            previous = services.store.get_job_run(run_id)
            if not previous.plan_id:
                raise ApiFailure(
                    409,
                    "RUN_PLAN_MISSING",
                    "原运行记录缺少固定计划，不能安全恢复；请重新盘点并确认计划",
                ) from None
            if not previous.scan_id:
                raise ApiFailure(
                    409,
                    "RUN_SCAN_MISSING",
                    "原运行记录缺少固定盘点，不能安全恢复；请重新盘点并确认计划",
                ) from None
            return launch_migration(
                previous.project_id,
                plan_id=previous.plan_id,
                scan_id=previous.scan_id,
            )
        services.store.update_job_run(run_id, status=LedgerRunStatus.RUNNING)
        return _job_payload(services, run_id, snapshot)

    @app.post("/api/v2/runs/{run_id}/stop")
    def stop_run(run_id: str, _: EmptyCommand) -> dict[str, Any]:
        snapshot = services.jobs.stop(run_id)
        return _job_payload(services, run_id, snapshot)

    @app.post("/api/v2/runs/{run_id}/retry")
    def retry_run(run_id: str, _: EmptyCommand) -> dict[str, Any]:
        previous = services.store.get_job_run(run_id)
        if not previous.plan_id:
            raise ApiFailure(
                409,
                "RUN_PLAN_MISSING",
                "原运行记录缺少固定计划，不能安全重试；请重新盘点并确认计划",
            )
        if not previous.scan_id:
            raise ApiFailure(
                409,
                "RUN_SCAN_MISSING",
                "原运行记录缺少固定盘点，不能安全重试；请重新盘点并确认计划",
            )
        return launch_migration(
            previous.project_id,
            plan_id=previous.plan_id,
            scan_id=previous.scan_id,
        )

    @app.post("/api/v2/projects/{project_id}/reconcile")
    def reconcile(project_id: str, _: EmptyCommand) -> dict[str, Any]:
        summary = services.reconciler().reconcile(project_id)
        return {
            "status": "complete",
            "checked": summary.checked,
            "matched": summary.matched,
            "missing": summary.missing,
            "missing_remote": summary.missing,
            "conflicts": summary.conflicts,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    @app.get("/api/v2/projects/{project_id}/audit", response_model=None)
    def audit(
        project_id: str,
        format: str | None = Query(default=None, pattern="^(csv|json)$"),
    ) -> Response | dict[str, Any]:
        rows = _audit_rows(services, project_id)
        if format == "csv":
            return Response(
                export_audit_csv(rows),
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="folder2feishu-{project_id}.csv"'
                    )
                },
            )
        if format == "json":
            return Response(
                export_audit_json(rows),
                media_type="application/json; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="folder2feishu-{project_id}.json"'
                    )
                },
            )
        events = [
            {
                "id": str(event.id),
                "occurred_at": event.created_at.isoformat(),
                "level": (
                    "SUCCESS"
                    if event.level == AuditLevel.INFO
                    and event.event_type.endswith(("finished", "created"))
                    else event.level.value
                ),
                "stage": event.event_type,
                "relative_path": event.rel_path or None,
                "message": event.message,
                "evidence": json_safe_evidence(event.payload),
            }
            for event in services.store.list_audit(project_id)
        ]
        items = [
            {
                "id": action.id,
                "relative_path": action.source_rel_path or action.previous_rel_path,
                "status": action.state.value,
                "progress": 100 if action.state == MigrationState.DONE else 0,
                "attempts": 0,
                "error_code": (action.details or {}).get("last_error_type"),
                "error_message": (action.details or {}).get("last_error"),
                "updated_at": action.updated_at.isoformat(),
            }
            for action in services.store.list_plan_actions(project_id)
        ]
        return {"events": events, "items": items}

    root = static_root or bundled_path("frontend", "dist")
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> Response:
        if full_path.startswith(("api/", "oauth/")):
            return _error(404, "NOT_FOUND", "接口不存在")
        candidate = (root / full_path).resolve()
        if full_path and root.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "FRONTEND_NOT_BUILT",
                    "message": "前端资源尚未构建",
                }
            },
        )

    return app


def json_safe_evidence(value: dict[str, Any]) -> str | None:
    if not value:
        return None
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
