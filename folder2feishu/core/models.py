"""SQLAlchemy models for the durable v2 migration ledger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .enums import (
    ActionType,
    AuditLevel,
    InventoryState,
    IssueCode,
    IssueSeverity,
    ItemKind,
    MigrationState,
    ProjectStatus,
    RemoteStatus,
    RunStatus,
    RunType,
    UploadStatus,
)

SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def enum_type(enum_class: type, name: str) -> SAEnum:
    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda values: [item.value for item in values],
    )


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    singleton: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_root: Mapped[str] = mapped_column(Text, nullable=False)
    target_wiki_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_space_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    target_parent_node_token: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    wrapper_name: Mapped[str] = mapped_column(String(250), nullable=False, default="")
    identity_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    incremental_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="safe_incremental"
    )
    status: Mapped[ProjectStatus] = mapped_column(
        enum_type(ProjectStatus, "project_status"),
        nullable=False,
        default=ProjectStatus.NEW,
    )
    current_scan_id: Mapped[str | None] = mapped_column(String(32))
    scan_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint("project_id", "rel_path", name="uq_inventory_project_path"),
        Index("ix_inventory_project_seen", "project_id", "last_seen_scan_id"),
        Index("ix_inventory_project_identity", "project_id", "file_identity"),
        Index("ix_inventory_project_hash", "project_id", "sha256"),
        Index("ix_inventory_project_present_kind", "project_id", "present", "kind"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ItemKind] = mapped_column(
        enum_type(ItemKind, "inventory_item_kind"), nullable=False
    )
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    parent_rel_path: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(1024), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    file_identity: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int | None] = mapped_column(BigInteger)
    mtime_ns: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    file_attributes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_offline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_recall_on_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_recall_on_data_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    state: Mapped[InventoryState] = mapped_column(
        enum_type(InventoryState, "inventory_state"),
        nullable=False,
        default=InventoryState.DISCOVERED,
    )
    first_seen_scan_id: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_scan_id: Mapped[str] = mapped_column(String(32), nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ScanIssue(Base):
    __tablename__ = "scan_issues"
    __table_args__ = (
        Index("ix_scan_issues_project_scan", "project_id", "scan_id"),
        Index("ix_scan_issues_severity", "project_id", "severity"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    scan_id: Mapped[str] = mapped_column(String(32), nullable=False)
    inventory_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="SET NULL")
    )
    rel_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    code: Mapped[IssueCode] = mapped_column(enum_type(IssueCode, "scan_issue_code"), nullable=False)
    severity: Mapped[IssueSeverity] = mapped_column(
        enum_type(IssueSeverity, "scan_issue_severity"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class RemoteMapping(Base):
    __tablename__ = "remote_mappings"
    __table_args__ = (
        Index("ix_mapping_project_path", "project_id", "last_source_rel_path"),
        Index("ix_mapping_project_identity", "project_id", "source_file_identity"),
        Index("ix_mapping_project_hash", "project_id", "source_sha256"),
        Index("ix_mapping_project_status", "project_id", "remote_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    inventory_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="SET NULL")
    )
    item_kind: Mapped[ItemKind] = mapped_column(
        enum_type(ItemKind, "remote_item_kind"), nullable=False
    )
    last_source_rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_file_identity: Mapped[str | None] = mapped_column(String(128))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_size: Mapped[int | None] = mapped_column(BigInteger)
    wiki_space_id: Mapped[str] = mapped_column(String(128), nullable=False)
    wiki_node_token: Mapped[str] = mapped_column(String(128), nullable=False)
    object_token: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    remote_parent_node_token: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    remote_title: Mapped[str] = mapped_column(String(1024), nullable=False)
    remote_status: Mapped[RemoteStatus] = mapped_column(
        enum_type(RemoteStatus, "remote_status"),
        nullable=False,
        default=RemoteStatus.UNVERIFIED,
    )
    conflict_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        "reconciled_at", DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class PlannedAction(Base):
    __tablename__ = "planned_actions"
    __table_args__ = (
        Index("ix_actions_project_plan", "project_id", "plan_id", "order_index"),
        Index("ix_actions_plan_type", "plan_id", "action_type"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[str] = mapped_column(String(32), nullable=False)
    inventory_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="SET NULL")
    )
    remote_mapping_id: Mapped[str | None] = mapped_column(
        ForeignKey("remote_mappings.id", ondelete="SET NULL")
    )
    action_type: Mapped[ActionType] = mapped_column(
        enum_type(ActionType, "planned_action_type"), nullable=False
    )
    state: Mapped[MigrationState] = mapped_column(
        enum_type(MigrationState, "planned_action_state"),
        nullable=False,
        default=MigrationState.PLANNED,
    )
    source_rel_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    previous_rel_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    destination_rel_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    drive_file_token: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    move_task_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    wiki_node_token: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    object_token: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        UniqueConstraint("upload_id", name="uq_upload_session_upload_id"),
        Index("ix_upload_action", "planned_action_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    planned_action_id: Mapped[str] = mapped_column(
        ForeignKey("planned_actions.id", ondelete="CASCADE"), nullable=False
    )
    upload_id: Mapped[str] = mapped_column(String(256), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    staging_parent_token: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    staging_name: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    drive_file_token: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    move_task_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    wiki_node_token: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    object_token: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    part_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_parts: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_parts: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[UploadStatus] = mapped_column(
        enum_type(UploadStatus, "upload_status"),
        nullable=False,
        default=UploadStatus.PREPARED,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_runs_project_created", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_type: Mapped[RunType] = mapped_column(enum_type(RunType, "job_run_type"), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        enum_type(RunStatus, "job_run_status"),
        nullable=False,
        default=RunStatus.QUEUED,
    )
    scan_id: Mapped[str | None] = mapped_column(String(32))
    plan_id: Mapped[str | None] = mapped_column(String(32))
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_project_created", "project_id", "created_at"),
        Index("ix_audit_run_created", "job_run_id", "created_at"),
    )

    # SQLite only auto-increments a primary key whose declared type is exactly
    # INTEGER (not BIGINT).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    job_run_id: Mapped[str | None] = mapped_column(ForeignKey("job_runs.id", ondelete="SET NULL"))
    planned_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("planned_actions.id", ondelete="SET NULL")
    )
    level: Mapped[AuditLevel] = mapped_column(
        enum_type(AuditLevel, "audit_level"), nullable=False, default=AuditLevel.INFO
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class JobLease(Base):
    __tablename__ = "job_leases"
    __table_args__ = (
        UniqueConstraint("project_id", "lease_name", name="uq_job_lease_project_name"),
        Index("ix_job_lease_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    lease_name: Mapped[str] = mapped_column(String(80), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
