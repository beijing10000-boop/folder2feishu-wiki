from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .settings import DEFAULT_SCOPES


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)


class WriteApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(ApiModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(ApiModel):
    error: ErrorDetail


class HealthResponse(ApiModel):
    status: str = "ok"
    version: str
    database: str = "ok"


class SettingsRead(ApiModel):
    app_id: str = ""
    redirect_uri: str
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    secret_configured: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    upload_qps: float = 4.0
    wiki_calls_per_minute: int = 90
    daily_upload_budget: int = 0


class SettingsUpdate(WriteApiModel):
    app_id: str
    app_secret: str | None = Field(default=None, min_length=1)
    redirect_uri: str = "http://localhost:8000/oauth/callback"
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    upload_qps: float = Field(default=4.0, gt=0, le=4.0)
    wiki_calls_per_minute: int = Field(default=90, ge=1, le=90)
    # Kept in the wire format for rolling upgrades; the service stores 0
    # and applies no application-side daily call cap.
    daily_upload_budget: int = Field(default=0, ge=0, le=0)


class AuthStatus(ApiModel):
    configured: bool = False
    authorized: bool = False
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)
    missing_scopes: list[str] = Field(default_factory=list)
    message: str = ""


class AuthStartResponse(ApiModel):
    authorization_url: str


class VerifyCommand(WriteApiModel):
    pass


class SourceVerifyRequest(WriteApiModel):
    source_root: str = Field(min_length=1)


class TargetVerifyRequest(WriteApiModel):
    target_wiki_url: str = Field(min_length=1)


class VerificationResponse(ApiModel):
    ok: bool
    kind: Literal["app", "oauth", "source", "target"]
    message: str
    details: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ProjectMode(StrEnum):
    SAFE_INCREMENTAL = "safe_incremental"


class ProjectCreate(WriteApiModel):
    name: str = Field(min_length=1, max_length=120)
    source_root: str = Field(min_length=1)
    target_wiki_url: str = Field(min_length=1)
    create_wrapper: bool = True
    wrapper_name: str | None = Field(default=None, max_length=250)
    mode: ProjectMode = ProjectMode.SAFE_INCREMENTAL

    @field_validator("source_root")
    @classmethod
    def reject_empty_source(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("源目录不能为空")
        return value


class ProjectUpdate(WriteApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    source_root: str | None = None
    target_wiki_url: str | None = None
    create_wrapper: bool | None = None
    wrapper_name: str | None = Field(default=None, max_length=250)


class ProjectRead(ApiModel):
    id: str
    name: str
    source_root: str
    target_wiki_url: str
    target_space_id: str = ""
    target_parent_token: str = ""
    wrapper_name: str = ""
    mode: ProjectMode = ProjectMode.SAFE_INCREMENTAL
    last_run_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class InventoryCounts(ApiModel):
    directories: int = 0
    files: int = 0
    bytes: int = 0
    offline: int = 0
    empty: int = 0
    unreadable: int = 0
    too_long: int = 0
    too_deep: int = 0


class ScanIssueRead(ApiModel):
    code: str
    severity: str
    relative_path: str
    message: str
    blocking: bool = False


class ScanResponse(ApiModel):
    run_id: str | None = None
    status: str = "idle"
    complete: bool = False
    cancel_requested: bool = False
    counts: InventoryCounts = Field(default_factory=InventoryCounts)
    issues: list[ScanIssueRead] = Field(default_factory=list)
    scanned_items: int = 0
    current_path: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PreflightCheck(ApiModel):
    key: str
    label: str
    status: str
    message: str
    blocking: bool = False


class PreflightResponse(ApiModel):
    ready: bool = False
    counts: InventoryCounts = Field(default_factory=InventoryCounts)
    checks: list[PreflightCheck] = Field(default_factory=list)
    issues: list[ScanIssueRead] = Field(default_factory=list)
    estimated_upload_calls: int = 0
    estimated_days: int = 0


class PlanActionType(StrEnum):
    CREATE_FOLDER = "create_folder"
    UPLOAD = "upload"
    MOVE = "move"
    RENAME = "rename"
    VERSION_UPDATE = "version_update"
    REPORT_MISSING = "report_missing"
    SKIP = "skip"
    CONFLICT = "conflict"


class PlanItemRead(ApiModel):
    id: str
    action: PlanActionType
    relative_path: str
    previous_path: str | None = None
    size: int = 0
    reason: str = ""
    blocking: bool = False


class PlanResponse(ApiModel):
    project_id: str
    status: str = "draft"
    counts: dict[str, int] = Field(default_factory=dict)
    items: list[PlanItemRead] = Field(default_factory=list)
    blocking_conflicts: int = 0
    estimated_upload_calls: int = 0
    estimated_days: int = 0


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    QUOTA_PAUSED = "quota_paused"


class QuotaStatus(ApiModel):
    used: int = 0
    budget: int = 0
    reset_at: datetime | None = None


class RunRead(ApiModel):
    run_id: str
    project_id: str
    status: RunStatus = RunStatus.QUEUED
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    current_item: str = ""
    progress: float = 0.0
    eta_seconds: int | None = None
    quota: QuotaStatus = Field(default_factory=QuotaStatus)
    errors: list[ScanIssueRead] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ReconcileResponse(ApiModel):
    status: str
    checked: int = 0
    matched: int = 0
    missing_remote: int = 0
    moved_remote: int = 0
    conflicts: list[ScanIssueRead] = Field(default_factory=list)
