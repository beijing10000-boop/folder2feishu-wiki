"""Localhost-only FastAPI application for the Windows operations console."""

from __future__ import annotations

import logging
import time
import uuid
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
from .job_control import HeartbeatPump, JobSnapshot
from .observability import METRICS, request_id_var
from .quota import DailyQuotaStore
from .runtime import bundled_path
from .runtime_logs import read_runtime_logs
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
    latest_run = next(
        (
            run
            for run in services.store.list_job_runs(project_id, limit=20)
            if run.run_type == RunType.SCAN
        ),
        None,
    )
    issues = services.store.list_issues(project_id, scan_id=project.current_scan_id)
    summary = services.inventory_summary(project_id)
    durable_status = latest_run.status.value if latest_run else "IDLE"
    status = {
        "QUEUED": "PENDING",
        "RUNNING": "RUNNING",
        "PAUSED": "PAUSED",
        "INTERRUPTED": "INTERRUPTED",
        "COMPLETE": "COMPLETED",
        "FAILED": "FAILED",
        "CANCELLED": "STOPPED",
    }.get(durable_status, durable_status)
    return {
        "scan_id": project.current_scan_id or (latest_run.scan_id if latest_run else "") or "",
        "run_id": latest_run.id if latest_run else None,
        "status": status,
        "complete": project.scan_complete,
        "cancel_requested": bool(latest_run and latest_run.cancel_requested),
        "summary": summary,
        "counts": summary,
        "checks": [_issue_payload(issue) for issue in issues],
        "issues": [_issue_payload(issue) for issue in issues],
        # The complete tree has its own endpoint. Materializing tens of
        # thousands of nodes here made every progress poll and application
        # restore wait for the largest payload in the project.
        "tree": [],
        "scanned_items": latest_run.completed_items if latest_run else 0,
        "current_path": latest_run.current_item if latest_run else "",
        "stage": latest_run.current_stage if latest_run else "IDLE",
        "last_message": latest_run.last_message if latest_run else "",
        "heartbeat_at": (
            latest_run.heartbeat_at.isoformat() if latest_run and latest_run.heartbeat_at else None
        ),
        "started_at": latest_run.started_at.isoformat()
        if latest_run and latest_run.started_at
        else None,
        "finished_at": latest_run.finished_at.isoformat()
        if latest_run and latest_run.finished_at
        else None,
    }


def _job_payload(
    services: ApplicationServices,
    run_id: str,
    snapshot: JobSnapshot | None = None,
) -> dict[str, Any]:
    run = services.store.get_job_run(run_id)
    quota = DailyQuotaStore(
        services.paths.quota,
        budget=services.settings_store.load().daily_upload_budget,
    ).snapshot()
    status_map = {
        LedgerRunStatus.QUEUED: "IDLE",
        LedgerRunStatus.RUNNING: "RUNNING",
        LedgerRunStatus.PAUSED: "PAUSED",
        LedgerRunStatus.INTERRUPTED: "INTERRUPTED",
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
    started_at = run.started_at
    if started_at and started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    elapsed_seconds = (
        max(0, int((datetime.now(UTC) - started_at).total_seconds())) if started_at else 0
    )
    run_summary = dict(run.summary or {})
    processed = run.completed_items + run.failed_items
    item_eta_seconds = None
    if processed > 0 and run.total_items > processed and elapsed_seconds > 0:
        item_eta_seconds = int(elapsed_seconds / processed * (run.total_items - processed))
    active_uploads: list[dict[str, Any]] = []
    active_uploaded_bytes = 0
    if run.plan_id:
        for upload, action in services.store.list_active_upload_progress(
            run.project_id,
            run.plan_id,
            limit=max(4, int(run_summary.get("workers", 0))),
        ):
            completed_parts = len(set(upload.completed_parts or []))
            uploaded_bytes = min(upload.file_size, completed_parts * upload.part_size)
            active_uploaded_bytes += uploaded_bytes
            active_uploads.append(
                {
                    "action_id": action.id,
                    "relative_path": action.source_rel_path or action.destination_rel_path,
                    "status": upload.status.value,
                    "completed_parts": completed_parts,
                    "total_parts": upload.total_parts,
                    "uploaded_bytes": uploaded_bytes,
                    "total_bytes": upload.file_size,
                    "percent": (
                        round(completed_parts / upload.total_parts * 100, 1)
                        if upload.total_parts
                        else 0
                    ),
                    "attempts": upload.attempts,
                    "last_error": upload.last_error,
                    "updated_at": upload.updated_at.isoformat(),
                }
            )
    effective_bytes_completed = min(
        run.bytes_total,
        run.bytes_completed + active_uploaded_bytes,
    )
    byte_eta_seconds = None
    if (
        effective_bytes_completed > 0
        and run.bytes_total > effective_bytes_completed
        and elapsed_seconds > 0
    ):
        byte_eta_seconds = int(
            elapsed_seconds
            / effective_bytes_completed
            * (run.bytes_total - effective_bytes_completed)
        )
    # Mixed workloads contain many tiny files and a small number of very large
    # files. Item-count ETA is too optimistic at the large-file tail, while a
    # byte-only ETA can be optimistic for metadata-heavy tiny files. Report the
    # more conservative estimate and expose both components for diagnostics.
    eta_candidates = [value for value in (item_eta_seconds, byte_eta_seconds) if value is not None]
    eta_seconds = max(eta_candidates) if eta_candidates else None
    eta_basis = (
        "items_and_bytes"
        if item_eta_seconds is not None and byte_eta_seconds is not None
        else "bytes"
        if byte_eta_seconds is not None
        else "items"
        if item_eta_seconds is not None
        else "unavailable"
    )
    return {
        "id": run.id,
        "run_id": run.id,
        "project_id": run.project_id,
        "kind": run.run_type.value,
        "stage": run.current_stage,
        "state": state,
        "status": state,
        "total": run.total_items,
        "completed": run.completed_items,
        "succeeded": run.completed_items,
        "failed": run.failed_items,
        "skipped": run.skipped_items,
        "conflicts": int((run.summary or {}).get("conflicts", 0)),
        "current_path": run.current_item,
        "current_item": run.current_item,
        "last_message": run.last_message,
        "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
        "elapsed_seconds": elapsed_seconds,
        "retry_count": run.retry_count,
        "worker_count": int(run_summary.get("workers", 0)),
        "in_flight": int(run_summary.get("in_flight", 0)),
        "pause_requested": run.pause_requested,
        "cancel_requested": run.cancel_requested,
        "progress": {
            "total": run.total_items,
            "completed": run.completed_items,
            "failed": run.failed_items,
        },
        "bytes_total": run.bytes_total,
        "bytes_completed": effective_bytes_completed,
        "ledger_bytes_completed": run.bytes_completed,
        "eta_seconds": eta_seconds,
        "eta_item_seconds": item_eta_seconds,
        "eta_bytes_seconds": byte_eta_seconds,
        "eta_basis": eta_basis,
        "active_uploads": active_uploads,
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
        interrupted = services.store.interrupt_orphaned_job_runs(stale_after_seconds=0)
        if interrupted:
            LOGGER.warning("recovered %s orphaned durable task(s) on startup", interrupted)
        yield
        if owned_services:
            services.close()

    app = FastAPI(
        title="Folder2Feishu Drive",
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

    @app.middleware("http")
    async def request_observability(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("x-request-id", "").strip() or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration = time.perf_counter() - started
            METRICS.record_api(request.url.path, duration, status_code)
            log_method = LOGGER.warning if duration >= 1.0 else LOGGER.info
            log_method(
                "HTTP request completed",
                extra={
                    "path": request.url.path,
                    "duration_ms": round(duration * 1000, 2),
                },
            )
            request_id_var.reset(token)

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

    @app.get("/api/v2/metrics")
    def metrics() -> dict[str, Any]:
        snapshot = METRICS.snapshot()
        statuses = services.store.job_status_counts()
        snapshot["tasks"] = {
            "running": statuses.get("RUNNING", 0),
            "queued": statuses.get("QUEUED", 0),
            "paused": statuses.get("PAUSED", 0),
            "interrupted": statuses.get("INTERRUPTED", 0),
            "complete": statuses.get("COMPLETE", 0),
            "failed": statuses.get("FAILED", 0),
        }
        return snapshot

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
            daily_upload_budget=0,
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
            raise ValueError("云盘迁移必须创建同名根目录")
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

    @app.get("/api/v2/projects/{project_id}/tasks")
    def list_project_tasks(
        project_id: str,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        services.store.get_project(project_id)
        return [
            _job_payload(services, run.id)
            for run in services.store.list_job_runs(project_id, limit=limit)
        ]

    @app.patch("/api/v2/projects/{project_id}")
    def update_project(project_id: str, value: ProjectUpdate) -> dict[str, Any]:
        updates = value.model_dump(exclude_none=True)
        create_wrapper = updates.pop("create_wrapper", None)
        if create_wrapper is False:
            raise ValueError("云盘迁移必须保留同名根目录")
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
        active = services.store.find_active_job_run(project_id, RunType.SCAN)
        if active:
            return {"run_id": active.id, "status": active.status.value, "deduplicated": True}
        durable = services.store.create_job_run(
            project_id,
            RunType.SCAN,
            status=LedgerRunStatus.QUEUED,
            current_stage="QUEUED",
        )

        def worker(control: Any, update: Any) -> dict[str, Any]:
            with HeartbeatPump(
                lambda: services.store.update_job_run(durable.id, heartbeat_at=utc_now())
            ):
                result = services.scanner.scan(
                    project_id,
                    run_id=durable.id,
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
            job = services.jobs.start(project_id, "scan", worker, run_id=durable.id)
        except RuntimeError as exc:
            services.store.update_job_run(
                durable.id,
                status=LedgerRunStatus.FAILED,
                current_stage="FAILED",
                last_message="无法启动本机后台盘点任务",
                error=str(exc),
                finished_at=utc_now(),
            )
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
    def get_tree(
        project_id: str,
        parent: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        items, total = services.store.list_inventory_children(
            project_id,
            parent_rel_path=parent,
            limit=limit,
            offset=offset,
        )
        child_counts = services.store.inventory_child_counts(
            project_id,
            (item.rel_path for item in items if item.kind.value == "FOLDER"),
        )
        return {
            "items": [
                {
                    "id": item.id,
                    "name": item.name,
                    "relative_path": item.rel_path,
                    "kind": item.kind.value.lower(),
                    "size": item.size or 0,
                    "status": item.state.value,
                    "child_count": child_counts.get(item.rel_path, 0),
                }
                for item in items
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(items) < total,
            "parent": parent,
        }

    @app.post("/api/v2/projects/{project_id}/plan")
    def post_plan(project_id: str, command: PlanCommand) -> dict[str, Any]:
        if command.confirmed:
            return services.confirm_latest_plan(project_id)
        active = services.store.find_active_job_run(project_id, RunType.PLAN)
        if active:
            return {**_job_payload(services, active.id), "accepted": True, "deduplicated": True}
        durable = services.store.create_job_run(
            project_id,
            RunType.PLAN,
            status=LedgerRunStatus.QUEUED,
            current_stage="QUEUED",
        )

        def worker(control: Any, update: Any) -> dict[str, Any]:
            with HeartbeatPump(
                lambda: services.store.update_job_run(durable.id, heartbeat_at=utc_now())
            ):
                try:
                    services.store.update_job_run(
                        durable.id,
                        status=LedgerRunStatus.RUNNING,
                        current_stage="PREFLIGHT",
                        last_message="正在检查权限、容量与目标云盘文件夹",
                        started_at=utc_now(),
                        heartbeat_at=utc_now(),
                    )
                    control.checkpoint()
                    preflight = services.preflight(project_id)
                    if not preflight.ready:
                        raise ApiFailure(
                            409,
                            "PREFLIGHT_BLOCKED",
                            "预检仍有阻断项，不能生成写入计划",
                            {"checks": preflight.checks},
                        )
                    services.store.update_job_run(
                        durable.id,
                        current_stage="PLANNING",
                        last_message="正在生成安全增量差异计划",
                        heartbeat_at=utc_now(),
                    )
                    control.checkpoint()

                    def plan_progress(completed: int, total: int, current: str) -> None:
                        services.store.update_job_run(
                            durable.id,
                            total_items=total,
                            completed_items=completed,
                            current_item=current,
                            last_message=f"正在生成差异计划：{completed:,} / {total:,}",
                            heartbeat_at=utc_now(),
                        )
                        update(
                            completed=completed,
                            total=total,
                            current_item=current,
                        )

                    result = services.planner.build(
                        project_id,
                        run_id=durable.id,
                        progress=plan_progress,
                        cancel=lambda: control.stop_requested,
                    )
                    services.store.update_job_run(
                        durable.id,
                        status=LedgerRunStatus.COMPLETE,
                        current_stage="COMPLETED",
                        total_items=result.total_actions,
                        completed_items=result.total_actions,
                        last_message="差异计划已生成，等待用户确认",
                        summary={"plan_id": result.plan_id, "counts": result.counts},
                        heartbeat_at=utc_now(),
                        finished_at=utc_now(),
                    )
                    return asdict(result)
                except Exception as exc:
                    if control.stop_requested:
                        services.store.update_job_run(
                            durable.id,
                            status=LedgerRunStatus.CANCELLED,
                            current_stage="CANCELLED",
                            last_message="差异计划任务已由用户停止",
                            cancel_requested=True,
                            heartbeat_at=utc_now(),
                            finished_at=utc_now(),
                        )
                    else:
                        services.store.update_job_run(
                            durable.id,
                            status=LedgerRunStatus.FAILED,
                            current_stage="FAILED",
                            last_message=str(exc),
                            error=f"{type(exc).__name__}: {exc}",
                            heartbeat_at=utc_now(),
                            finished_at=utc_now(),
                        )
                    raise

        try:
            job = services.jobs.start(project_id, "plan", worker, run_id=durable.id)
        except Exception as exc:
            services.store.update_job_run(
                durable.id,
                status=LedgerRunStatus.FAILED,
                current_stage="FAILED",
                last_message="无法启动本机计划任务",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=utc_now(),
            )
            raise
        return {**_job_payload(services, durable.id, job), "accepted": True}

    @app.get("/api/v2/projects/{project_id}/plan")
    def get_plan(project_id: str) -> dict[str, Any]:
        return services.plan_payload(project_id)

    def launch_migration(
        project_id: str,
        *,
        plan_id: str | None = None,
        scan_id: str | None = None,
        existing_run_id: str | None = None,
    ) -> dict[str, Any]:
        project = services.store.get_project(project_id)
        active = services.store.find_active_job_run(project_id, RunType.MIGRATION)
        if active and active.id != existing_run_id:
            return {**_job_payload(services, active.id), "deduplicated": True}
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
        stats = services.store.plan_run_summary(project_id, selected_plan_id)
        if existing_run_id:
            durable = services.store.update_job_run(
                existing_run_id,
                status=LedgerRunStatus.QUEUED,
                total_items=stats["total"],
                bytes_total=stats["bytes_total"],
                current_stage="QUEUED",
                current_item="",
                last_message="任务已进入恢复队列",
                pause_requested=False,
                cancel_requested=False,
                heartbeat_at=utc_now(),
                finished_at=None,
                error="",
            )
        else:
            durable = services.store.create_job_run(
                project_id,
                RunType.MIGRATION,
                status=LedgerRunStatus.QUEUED,
                scan_id=scan_id or project.current_scan_id,
                plan_id=selected_plan_id,
                total_items=stats["total"],
                bytes_total=stats["bytes_total"],
                current_stage="QUEUED",
            )

        def worker(control: Any, update: Any) -> dict[str, Any]:
            try:
                result = services.executor().execute(
                    project_id,
                    run_id=durable.id,
                    control=control,
                    progress=update,
                )
                return asdict(result)
            except Exception as exc:
                current = services.store.get_job_run(durable.id)
                if current.status in {LedgerRunStatus.QUEUED, LedgerRunStatus.RUNNING}:
                    services.store.update_job_run(
                        durable.id,
                        status=LedgerRunStatus.FAILED,
                        current_stage="FAILED",
                        last_message="后台迁移任务异常退出",
                        heartbeat_at=utc_now(),
                        error=f"{type(exc).__name__}: {exc}",
                        finished_at=utc_now(),
                    )
                raise

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
        durable = services.store.get_job_run(run_id)
        heartbeat = durable.heartbeat_at
        if heartbeat and heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        if (
            snapshot is None
            and durable.status in {LedgerRunStatus.QUEUED, LedgerRunStatus.RUNNING}
            and (heartbeat is None or (datetime.now(UTC) - heartbeat).total_seconds() > 30)
        ):
            services.store.update_job_run(
                run_id,
                status=LedgerRunStatus.INTERRUPTED,
                current_stage="INTERRUPTED",
                last_message="任务心跳已中断，可从已落库断点继续",
                finished_at=utc_now(),
            )
        return _job_payload(services, run_id, snapshot)

    @app.get("/api/v2/runtime/logs")
    def runtime_logs(
        after: int | None = Query(default=None, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return read_runtime_logs(
            services.paths.logs / "folder2feishu.log",
            after=after,
            limit=limit,
        )

    @app.post("/api/v2/runs/{run_id}/pause")
    def pause_run(run_id: str, _: EmptyCommand) -> dict[str, Any]:
        run = services.store.get_job_run(run_id)
        try:
            snapshot = services.jobs.pause(run_id)
        except KeyError:
            snapshot = None
        services.store.update_job_run(
            run_id,
            status=LedgerRunStatus.PAUSED,
            current_stage="PAUSED",
            last_message="用户已请求安全暂停",
            pause_requested=True,
            heartbeat_at=utc_now(),
        )
        services.store.update_project(run.project_id, status=ProjectStatus.PAUSED)
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
                existing_run_id=previous.id,
            )
        services.store.update_job_run(run_id, status=LedgerRunStatus.RUNNING)
        return _job_payload(services, run_id, snapshot)

    @app.post("/api/v2/runs/{run_id}/stop")
    def stop_run(run_id: str, _: EmptyCommand) -> dict[str, Any]:
        run = services.store.get_job_run(run_id)
        services.store.update_job_run(
            run_id,
            cancel_requested=True,
            last_message="正在等待当前网络操作结束后停止",
            heartbeat_at=utc_now(),
        )
        try:
            snapshot = services.jobs.stop(run_id)
        except KeyError:
            snapshot = None
            services.store.update_job_run(
                run_id,
                status=LedgerRunStatus.CANCELLED,
                current_stage="CANCELLED",
                current_item="",
                last_message="任务已停止",
                finished_at=utc_now(),
            )
            services.store.update_project(run.project_id, status=ProjectStatus.PAUSED)
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
            existing_run_id=previous.id,
        )

    @app.post("/api/v2/projects/{project_id}/reconcile")
    def reconcile(project_id: str, _: EmptyCommand) -> dict[str, Any]:
        active = services.store.find_active_job_run(project_id, RunType.RECONCILIATION)
        if active:
            return {**_job_payload(services, active.id), "accepted": True, "deduplicated": True}
        durable = services.store.create_job_run(
            project_id,
            RunType.RECONCILIATION,
            status=LedgerRunStatus.QUEUED,
            current_stage="QUEUED",
        )

        def worker(control: Any, update: Any) -> dict[str, Any]:
            summary = services.reconciler().reconcile(
                project_id,
                run_id=durable.id,
                control=control,
                progress=update,
            )
            return asdict(summary)

        try:
            job = services.jobs.start(
                project_id,
                "reconciliation",
                worker,
                run_id=durable.id,
            )
        except Exception as exc:
            services.store.update_job_run(
                durable.id,
                status=LedgerRunStatus.FAILED,
                current_stage="FAILED",
                last_message="无法启动远端对账任务",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=utc_now(),
            )
            raise
        return {**_job_payload(services, durable.id, job), "accepted": True}

    @app.get("/api/v2/projects/{project_id}/audit", response_model=None)
    def audit(
        project_id: str,
        format: str | None = Query(default=None, pattern="^(csv|json)$"),
        after_id: int | None = Query(default=None, ge=0),
        limit: int = Query(default=200, ge=1, le=2_000),
    ) -> Response | dict[str, Any]:
        if format == "csv":
            rows = _audit_rows(services, project_id)
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
            rows = _audit_rows(services, project_id)
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
            for event in services.store.list_audit(
                project_id,
                after_id=after_id,
                limit=limit,
                latest=after_id is None,
            )
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
            for action in services.store.list_plan_action_statuses(project_id, limit=limit)
        ]
        return {
            "events": events,
            "items": items,
            "next_after_id": int(events[-1]["id"] or 0) if events else after_id,
        }

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
