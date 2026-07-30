"""SQLite/SQLAlchemy storage facade used by scanners and orchestrators."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import Engine, create_engine, delete, event, func, inspect, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .enums import (
    AuditLevel,
    ItemKind,
    RemoteStatus,
    RunStatus,
    RunType,
    UploadStatus,
)
from .models import (
    SCHEMA_VERSION,
    AuditEvent,
    Base,
    InventoryItem,
    JobLease,
    JobRun,
    PlannedAction,
    Project,
    RemoteMapping,
    ScanIssue,
    SchemaVersion,
    UploadSession,
    new_id,
    utc_now,
)

ModelT = TypeVar("ModelT", bound=Base)


class SchemaVersionError(RuntimeError):
    pass


class LeaseBusyError(RuntimeError):
    def __init__(self, lease_name: str, owner_id: str, expires_at: datetime):
        super().__init__(
            f"lease {lease_name!r} is held by {owner_id!r} until {expires_at.isoformat()}"
        )
        self.lease_name = lease_name
        self.owner_id = owner_id
        self.expires_at = expires_at


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def model_dict(instance: Base) -> dict[str, Any]:
    """Serialize an ORM model without loading relationships."""

    result: dict[str, Any] = {}
    for column in inspect(instance).mapper.column_attrs:
        value = getattr(instance, column.key)
        if isinstance(value, Enum):
            value = value.value
        elif isinstance(value, datetime):
            value = _as_utc(value).isoformat()
        result[column.key] = value
    return result


class CoreStore:
    """Single entry point for the v2 database contract.

    Returned ORM objects are detached safely because sessions use
    ``expire_on_commit=False``.  Callers that need JSON can use
    :meth:`serialize`.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 30_000):
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        is_memory = str(path) == ":memory:"
        if not is_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connect_args = {
            "check_same_thread": False,
            "timeout": max(1.0, self.busy_timeout_ms / 1000),
        }
        kwargs: dict[str, Any] = {}
        if is_memory:
            kwargs["poolclass"] = StaticPool
            database_url = "sqlite+pysqlite:///:memory:"
        else:
            database_url = f"sqlite+pysqlite:///{self.path.as_posix()}"
        self.engine: Engine = create_engine(
            database_url,
            connect_args=connect_args,
            future=True,
            **kwargs,
        )

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            if not is_memory:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        self._session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)
        self.initialize()

    def initialize(self) -> int:
        Base.metadata.create_all(self.engine)
        with self.session() as session:
            row = session.get(SchemaVersion, 1)
            if row is None:
                session.add(SchemaVersion(singleton=1, version=SCHEMA_VERSION))
            elif row.version > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database schema {row.version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            elif row.version < SCHEMA_VERSION:
                self._migrate(session, row.version, SCHEMA_VERSION)
            return SCHEMA_VERSION

    @staticmethod
    def _migrate(session: Session, current: int, target: int) -> None:
        # v1 is the first clean-room schema.  The explicit gate is kept here so
        # future versions add ordered, transactional migrations instead of
        # silently relying on create_all.
        if current != target:
            raise SchemaVersionError(f"no migration path from schema {current} to {target}")

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def serialize(instance: Base) -> dict[str, Any]:
        return model_dict(instance)

    def pragmas(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            return {
                "journal_mode": connection.execute(text("PRAGMA journal_mode")).scalar_one(),
                "busy_timeout": connection.execute(text("PRAGMA busy_timeout")).scalar_one(),
                "foreign_keys": connection.execute(text("PRAGMA foreign_keys")).scalar_one(),
            }

    # Projects
    def create_project(
        self,
        *,
        name: str,
        source_root: str | Path,
        target_wiki_url: str = "",
        target_space_id: str = "",
        target_parent_node_token: str = "",
        wrapper_name: str | None = None,
        identity_key: str = "",
        incremental_policy: str = "safe_incremental",
        project_id: str | None = None,
    ) -> Project:
        source = Path(source_root).expanduser().absolute()
        project = Project(
            id=project_id or new_id(),
            name=name.strip(),
            source_root=str(source),
            target_wiki_url=target_wiki_url.strip(),
            target_space_id=target_space_id.strip(),
            target_parent_node_token=target_parent_node_token.strip(),
            wrapper_name=(wrapper_name or source.name).strip(),
            identity_key=identity_key,
            incremental_policy=incremental_policy,
        )
        if not project.name:
            raise ValueError("project name must not be empty")
        with self.session() as session:
            session.add(project)
        return project

    def get_project(self, project_id: str) -> Project:
        with self.session() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(f"unknown project: {project_id}")
            return project

    def list_projects(self) -> list[Project]:
        with self.session() as session:
            return list(session.scalars(select(Project).order_by(Project.created_at)))

    def update_project(self, project_id: str, **values: Any) -> Project:
        allowed = {
            "name",
            "source_root",
            "target_wiki_url",
            "target_space_id",
            "target_parent_node_token",
            "wrapper_name",
            "identity_key",
            "incremental_policy",
            "status",
            "current_scan_id",
            "scan_complete",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unsupported project fields: {sorted(unknown)}")
        with self.session() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(f"unknown project: {project_id}")
            for key, value in values.items():
                setattr(project, key, value)
            project.updated_at = utc_now()
            session.flush()
            return project

    # Inventory and scan issues
    def replace_inventory(
        self,
        project_id: str,
        scan_id: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        mark_absent: bool = True,
    ) -> int:
        """Bulk-upsert inventory facts for one scan.

        Scanner implementations can call this repeatedly with
        ``mark_absent=False`` after making one initial call with an empty batch
        and ``mark_absent=True``.
        """

        values = list(rows)
        now = utc_now()
        with self.session() as session:
            if mark_absent:
                session.query(InventoryItem).filter(InventoryItem.project_id == project_id).update(
                    {
                        InventoryItem.present: False,
                        InventoryItem.updated_at: now,
                    },
                    synchronize_session=False,
                )
            if not values:
                return 0
            prepared = []
            for source in values:
                row = dict(source)
                row.setdefault("id", new_id())
                row["project_id"] = project_id
                row["last_seen_scan_id"] = scan_id
                row.setdefault("first_seen_scan_id", scan_id)
                row["present"] = True
                row.setdefault("discovered_at", now)
                row["updated_at"] = now
                prepared.append(row)
            statement = sqlite_insert(InventoryItem).values(prepared)
            excluded = statement.excluded
            statement = statement.on_conflict_do_update(
                index_elements=["project_id", "rel_path"],
                set_={
                    "kind": excluded.kind,
                    "parent_rel_path": excluded.parent_rel_path,
                    "name": excluded.name,
                    "depth": excluded.depth,
                    "file_identity": excluded.file_identity,
                    "size": excluded.size,
                    "mtime_ns": excluded.mtime_ns,
                    "sha256": excluded.sha256,
                    "file_attributes": excluded.file_attributes,
                    "is_offline": excluded.is_offline,
                    "is_recall_on_open": excluded.is_recall_on_open,
                    "is_recall_on_data_access": excluded.is_recall_on_data_access,
                    "present": True,
                    "state": excluded.state,
                    "last_seen_scan_id": scan_id,
                    "updated_at": now,
                },
            )
            session.execute(statement)
        return len(prepared)

    def list_inventory(
        self,
        project_id: str,
        *,
        present: bool | None = None,
        kind: ItemKind | None = None,
    ) -> list[InventoryItem]:
        statement = select(InventoryItem).where(InventoryItem.project_id == project_id)
        if present is not None:
            statement = statement.where(InventoryItem.present == present)
        if kind is not None:
            statement = statement.where(InventoryItem.kind == kind)
        statement = statement.order_by(InventoryItem.depth, InventoryItem.rel_path)
        with self.session() as session:
            return list(session.scalars(statement))

    def add_scan_issues(
        self, project_id: str, scan_id: str, issues: Iterable[Mapping[str, Any]]
    ) -> int:
        prepared = []
        for source in issues:
            row = dict(source)
            row.setdefault("id", new_id())
            row["project_id"] = project_id
            row["scan_id"] = scan_id
            prepared.append(row)
        if not prepared:
            return 0
        with self.session() as session:
            session.execute(sqlite_insert(ScanIssue).values(prepared))
        return len(prepared)

    def list_issues(
        self,
        project_id: str,
        *,
        scan_id: str | None = None,
    ) -> list[ScanIssue]:
        statement = select(ScanIssue).where(ScanIssue.project_id == project_id)
        if scan_id is not None:
            statement = statement.where(ScanIssue.scan_id == scan_id)
        statement = statement.order_by(ScanIssue.created_at, ScanIssue.rel_path)
        with self.session() as session:
            return list(session.scalars(statement))

    def inventory_summary(self, project_id: str) -> dict[str, Any]:
        with self.session() as session:
            item_rows = session.execute(
                select(
                    InventoryItem.kind,
                    InventoryItem.present,
                    func.count(InventoryItem.id),
                    func.coalesce(func.sum(InventoryItem.size), 0),
                )
                .where(InventoryItem.project_id == project_id)
                .group_by(InventoryItem.kind, InventoryItem.present)
            ).all()
            issue_rows = session.execute(
                select(ScanIssue.severity, func.count(ScanIssue.id))
                .where(ScanIssue.project_id == project_id)
                .group_by(ScanIssue.severity)
            ).all()
        items = {
            f"{kind.value.lower()}_{'present' if present else 'missing'}": {
                "count": count,
                "bytes": int(size or 0),
            }
            for kind, present, count, size in item_rows
        }
        issues = {severity.value.lower(): count for severity, count in issue_rows}
        return {"items": items, "issues": issues}

    # Plans
    def save_plan(
        self,
        project_id: str,
        plan_id: str,
        actions: Iterable[Mapping[str, Any]],
    ) -> list[PlannedAction]:
        objects: list[PlannedAction] = []
        for index, source in enumerate(actions):
            values = dict(source)
            values.setdefault("id", new_id())
            values["project_id"] = project_id
            values["plan_id"] = plan_id
            values.setdefault("order_index", index)
            objects.append(PlannedAction(**values))
        with self.session() as session:
            session.add_all(objects)
        return objects

    def list_plan_actions(
        self, project_id: str, *, plan_id: str | None = None
    ) -> list[PlannedAction]:
        with self.session() as session:
            if plan_id is None:
                plan_id = session.scalar(
                    select(PlannedAction.plan_id)
                    .where(PlannedAction.project_id == project_id)
                    .order_by(PlannedAction.created_at.desc())
                    .limit(1)
                )
            if plan_id is None:
                return []
            return list(
                session.scalars(
                    select(PlannedAction)
                    .where(
                        PlannedAction.project_id == project_id,
                        PlannedAction.plan_id == plan_id,
                    )
                    .order_by(PlannedAction.order_index)
                )
            )

    def get_plan_action(self, action_id: str) -> PlannedAction:
        with self.session() as session:
            action = session.get(PlannedAction, action_id)
            if action is None:
                raise KeyError(f"unknown planned action: {action_id}")
            return action

    def update_plan_action(
        self,
        action_id: str,
        *,
        merge_details: Mapping[str, Any] | None = None,
        **values: Any,
    ) -> PlannedAction:
        allowed = {
            "state",
            "reason",
            "source_rel_path",
            "previous_rel_path",
            "destination_rel_path",
            "order_index",
            "details",
            "drive_file_token",
            "move_task_id",
            "wiki_node_token",
            "object_token",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unsupported planned action fields: {sorted(unknown)}")
        with self.session() as session:
            action = session.get(PlannedAction, action_id)
            if action is None:
                raise KeyError(f"unknown planned action: {action_id}")
            for key, value in values.items():
                setattr(action, key, value)
            if merge_details:
                action.details = {**(action.details or {}), **dict(merge_details)}
            action.updated_at = utc_now()
            session.flush()
            return action

    # Remote mappings
    def upsert_remote_mapping(self, **values: Any) -> RemoteMapping:
        mapping_id = values.get("id")
        project_id = values.get("project_id")
        source_path = values.get("last_source_rel_path")
        with self.session() as session:
            mapping = None
            if mapping_id:
                mapping = session.get(RemoteMapping, mapping_id)
            if mapping is None and project_id and source_path is not None:
                mapping = session.scalar(
                    select(RemoteMapping).where(
                        RemoteMapping.project_id == project_id,
                        RemoteMapping.last_source_rel_path == source_path,
                        RemoteMapping.is_current.is_(True),
                    )
                )
            if mapping is None:
                mapping = RemoteMapping(**values)
                session.add(mapping)
            else:
                for key, value in values.items():
                    if key != "id":
                        setattr(mapping, key, value)
                mapping.updated_at = utc_now()
            session.flush()
            return mapping

    def find_current_remote_mapping(
        self,
        project_id: str,
        *,
        inventory_item_id: str | None = None,
        rel_path: str | None = None,
    ) -> RemoteMapping | None:
        if not inventory_item_id and rel_path is None:
            raise ValueError("inventory_item_id or rel_path is required")
        statement = select(RemoteMapping).where(
            RemoteMapping.project_id == project_id,
            RemoteMapping.is_current.is_(True),
        )
        if inventory_item_id:
            statement = statement.where(RemoteMapping.inventory_item_id == inventory_item_id)
        if rel_path is not None:
            statement = statement.where(RemoteMapping.last_source_rel_path == rel_path)
        with self.session() as session:
            return session.scalar(statement.order_by(RemoteMapping.updated_at.desc()))

    def mark_remote_mapping_historical(
        self,
        mapping_id: str,
        *,
        status: RemoteStatus = RemoteStatus.ARCHIVED,
    ) -> RemoteMapping:
        with self.session() as session:
            mapping = session.get(RemoteMapping, mapping_id)
            if mapping is None:
                raise KeyError(f"unknown remote mapping: {mapping_id}")
            mapping.is_current = False
            mapping.remote_status = status
            mapping.updated_at = utc_now()
            session.flush()
            return mapping

    def get_remote_mapping(self, mapping_id: str) -> RemoteMapping:
        with self.session() as session:
            mapping = session.get(RemoteMapping, mapping_id)
            if mapping is None:
                raise KeyError(f"unknown remote mapping: {mapping_id}")
            return mapping

    def list_remote_mappings(
        self, project_id: str, *, current_only: bool = True
    ) -> list[RemoteMapping]:
        statement = select(RemoteMapping).where(RemoteMapping.project_id == project_id)
        if current_only:
            statement = statement.where(RemoteMapping.is_current.is_(True))
        with self.session() as session:
            return list(session.scalars(statement))

    # Multipart upload sessions
    def upsert_upload_session(self, **values: Any) -> UploadSession:
        upload_id = values.get("upload_id")
        session_id = values.get("id")
        action_id = values.get("planned_action_id")
        with self.session() as session:
            upload = session.get(UploadSession, session_id) if session_id else None
            if upload is None and upload_id:
                upload = session.scalar(
                    select(UploadSession).where(UploadSession.upload_id == upload_id)
                )
            if upload is None and not upload_id and action_id:
                upload = session.scalar(
                    select(UploadSession)
                    .where(UploadSession.planned_action_id == action_id)
                    .order_by(UploadSession.updated_at.desc())
                    .limit(1)
                )
            if upload is None:
                upload = UploadSession(**values)
                session.add(upload)
            else:
                for key, value in values.items():
                    if key != "id":
                        setattr(upload, key, value)
                upload.updated_at = utc_now()
            session.flush()
            return upload

    def get_upload_session(self, upload_id: str) -> UploadSession | None:
        with self.session() as session:
            return session.scalar(select(UploadSession).where(UploadSession.upload_id == upload_id))

    def get_upload_session_for_action(self, planned_action_id: str) -> UploadSession | None:
        with self.session() as session:
            return session.scalar(
                select(UploadSession)
                .where(UploadSession.planned_action_id == planned_action_id)
                .order_by(UploadSession.updated_at.desc())
                .limit(1)
            )

    def record_upload_progress(
        self,
        planned_action_id: str,
        *,
        completed_part: int | None = None,
        status: UploadStatus | None = None,
        drive_file_token: str | None = None,
        move_task_id: str | None = None,
        wiki_node_token: str | None = None,
        object_token: str | None = None,
        last_error: str | None = None,
        increment_attempts: bool = False,
    ) -> UploadSession:
        """Durably persist a chunk/upload/move checkpoint.

        ``completed_part`` is merged idempotently, making repeated callback
        delivery safe after a crash or timeout.
        """

        with self.session() as session:
            upload = session.scalar(
                select(UploadSession)
                .where(UploadSession.planned_action_id == planned_action_id)
                .order_by(UploadSession.updated_at.desc())
                .limit(1)
            )
            if upload is None:
                raise KeyError(f"no upload session for planned action: {planned_action_id}")
            if completed_part is not None:
                parts = set(upload.completed_parts or [])
                parts.add(int(completed_part))
                upload.completed_parts = sorted(parts)
            if status is not None:
                upload.status = status
            for field, value in {
                "drive_file_token": drive_file_token,
                "move_task_id": move_task_id,
                "wiki_node_token": wiki_node_token,
                "object_token": object_token,
                "last_error": last_error,
            }.items():
                if value is not None:
                    setattr(upload, field, value)
            if increment_attempts:
                upload.attempts += 1
            upload.updated_at = utc_now()
            session.flush()
            return upload

    # Runs and audit
    def create_job_run(
        self,
        project_id: str,
        run_type: RunType,
        *,
        status: RunStatus = RunStatus.QUEUED,
        scan_id: str | None = None,
        plan_id: str | None = None,
    ) -> JobRun:
        run = JobRun(
            project_id=project_id,
            run_type=run_type,
            status=status,
            scan_id=scan_id,
            plan_id=plan_id,
            started_at=utc_now() if status == RunStatus.RUNNING else None,
        )
        with self.session() as session:
            session.add(run)
        return run

    def update_job_run(self, run_id: str, **values: Any) -> JobRun:
        allowed = {
            "status",
            "scan_id",
            "plan_id",
            "total_items",
            "completed_items",
            "failed_items",
            "skipped_items",
            "summary",
            "error",
            "started_at",
            "finished_at",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        with self.session() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise KeyError(f"unknown job run: {run_id}")
            for key, value in values.items():
                setattr(run, key, value)
            session.flush()
            return run

    def get_job_run(self, run_id: str) -> JobRun:
        with self.session() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise KeyError(f"unknown job run: {run_id}")
            return run

    def list_job_runs(self, project_id: str, *, limit: int = 100) -> list[JobRun]:
        if limit < 1:
            return []
        with self.session() as session:
            return list(
                session.scalars(
                    select(JobRun)
                    .where(JobRun.project_id == project_id)
                    .order_by(JobRun.created_at.desc())
                    .limit(limit)
                )
            )

    def append_audit(
        self,
        project_id: str,
        event_type: str,
        message: str,
        *,
        level: AuditLevel = AuditLevel.INFO,
        job_run_id: str | None = None,
        planned_action_id: str | None = None,
        rel_path: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        entry = AuditEvent(
            project_id=project_id,
            job_run_id=job_run_id,
            planned_action_id=planned_action_id,
            level=level,
            event_type=event_type,
            message=message,
            rel_path=rel_path,
            payload=dict(payload or {}),
        )
        with self.session() as session:
            session.add(entry)
        return entry

    def list_audit(self, project_id: str, *, job_run_id: str | None = None) -> list[AuditEvent]:
        statement = select(AuditEvent).where(AuditEvent.project_id == project_id)
        if job_run_id:
            statement = statement.where(AuditEvent.job_run_id == job_run_id)
        statement = statement.order_by(AuditEvent.id)
        with self.session() as session:
            return list(session.scalars(statement))

    # Single-writer project leases
    def acquire_lease(
        self,
        project_id: str,
        lease_name: str,
        owner_id: str,
        *,
        ttl_seconds: int = 120,
        now: datetime | None = None,
    ) -> JobLease:
        current_time = _as_utc(now or utc_now())
        expires_at = current_time + timedelta(seconds=ttl_seconds)
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        try:
            with self.session() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                lease = session.scalar(
                    select(JobLease).where(
                        JobLease.project_id == project_id,
                        JobLease.lease_name == lease_name,
                    )
                )
                if lease is not None:
                    existing_expiry = _as_utc(lease.expires_at)
                    if lease.owner_id != owner_id and existing_expiry > current_time:
                        raise LeaseBusyError(lease_name, lease.owner_id, existing_expiry)
                    lease.owner_id = owner_id
                    lease.acquired_at = current_time
                    lease.heartbeat_at = current_time
                    lease.expires_at = expires_at
                else:
                    lease = JobLease(
                        project_id=project_id,
                        lease_name=lease_name,
                        owner_id=owner_id,
                        acquired_at=current_time,
                        heartbeat_at=current_time,
                        expires_at=expires_at,
                    )
                    session.add(lease)
                session.flush()
                return lease
        except IntegrityError as exc:
            raise LeaseBusyError(lease_name, "another process", expires_at) from exc

    def release_lease(self, project_id: str, lease_name: str, owner_id: str) -> bool:
        with self.session() as session:
            result = session.execute(
                delete(JobLease).where(
                    JobLease.project_id == project_id,
                    JobLease.lease_name == lease_name,
                    JobLease.owner_id == owner_id,
                )
            )
            return bool(getattr(result, "rowcount", 0))
