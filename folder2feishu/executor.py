"""Durable migration execution and remote reconciliation."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .core import (
    ActionType,
    AuditLevel,
    CoreStore,
    InventoryItem,
    ItemKind,
    LedgerPersistenceHooks,
    MigrationState,
    PlannedAction,
    Project,
    ProjectStatus,
    RemoteMapping,
    RemoteStatus,
    RunStatus,
    RunType,
)
from .core.models import utc_now
from .core.scanner import sha256_file
from .feishu import (
    DriveService,
    FeishuAmbiguousWriteError,
    FeishuError,
    ReconcileStatus,
    WikiMoveTaskFailedError,
    WikiService,
    deterministic_staging_name,
)
from .job_control import JobControl, JobStopped
from .quota import DailyQuotaExceeded, DailyQuotaStore


class MigrationBlocked(RuntimeError):
    """The plan or remote state requires an operator decision."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    run_id: str
    total: int
    completed: int
    skipped: int
    failed: int
    conflicts: int
    quota_paused: bool = False


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    checked: int
    matched: int
    missing: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class _ActionOutcome:
    action: PlannedAction
    item_bytes: int = 0
    completed: bool = False
    skipped: bool = False
    failed: int = 0
    conflicts: int = 0
    quota_paused: bool = False
    already_done: bool = False


ProgressCallback = Callable[..., None]
PROJECT_WRITE_LEASE = "migration"
PROJECT_LEASE_TTL_SECONDS = 120
PROJECT_LEASE_HEARTBEAT_SECONDS = 30.0
DEFAULT_MIGRATION_WORKERS = 4
MAX_QUEUED_ACTIONS_PER_WORKER = 2
RUN_PROGRESS_PERSIST_SECONDS = 1.0


def _parent_rel_path(rel_path: str) -> str | None:
    if rel_path == "":
        return None
    return rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""


def _safe_source_path(project: Project, item: InventoryItem) -> Path:
    root = Path(project.source_root).expanduser().resolve()
    candidate = root.joinpath(*PurePosixPath(item.rel_path).parts).resolve()
    if candidate != root and root not in candidate.parents:
        raise MigrationBlocked("源文件路径越出了项目根目录")
    return candidate


def _assert_fixed_oauth_identity(project: Project, drive: DriveService) -> None:
    if not project.identity_key:
        raise MigrationBlocked("项目尚未绑定固定 OAuth 用户，请重新完成预检")
    user_info = drive.get_current_user_info()
    current_user_id = str(user_info.get("user_id") or "")
    if not current_user_id:
        raise MigrationBlocked("飞书未返回当前 OAuth 用户的 user_id，请检查用户身份读取权限")
    if current_user_id != project.identity_key:
        raise MigrationBlocked("当前 OAuth 用户与项目绑定身份不一致，已在任何远端写入前停止")


class _ProjectLeaseHeartbeat:
    """Keep the cross-process project writer lease alive during long API calls."""

    def __init__(
        self,
        store: CoreStore,
        project_id: str,
        owner_id: str,
        *,
        run_id: str | None = None,
        ttl_seconds: int = PROJECT_LEASE_TTL_SECONDS,
        interval_seconds: float = PROJECT_LEASE_HEARTBEAT_SECONDS,
    ) -> None:
        if ttl_seconds <= 0 or interval_seconds <= 0 or interval_seconds >= ttl_seconds:
            raise ValueError("project lease heartbeat interval must be shorter than its TTL")
        self.store = store
        self.project_id = project_id
        self.owner_id = owner_id
        self.run_id = run_id
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._failure_lock = threading.Lock()

    def start(self) -> None:
        self.store.acquire_lease(
            self.project_id,
            PROJECT_WRITE_LEASE,
            self.owner_id,
            ttl_seconds=self.ttl_seconds,
        )
        if self.run_id:
            self.store.update_job_run(self.run_id, heartbeat_at=utc_now())
        self._thread = threading.Thread(
            target=self._run,
            name=f"folder2feishu-lease-{self.project_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def checkpoint(self) -> None:
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            raise RuntimeError("project writer lease heartbeat failed") from failure

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
        self.store.release_lease(
            self.project_id,
            PROJECT_WRITE_LEASE,
            self.owner_id,
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.store.acquire_lease(
                    self.project_id,
                    PROJECT_WRITE_LEASE,
                    self.owner_id,
                    ttl_seconds=self.ttl_seconds,
                )
                if self.run_id:
                    self.store.update_job_run(self.run_id, heartbeat_at=utc_now())
            except BaseException as exc:
                with self._failure_lock:
                    self._failure = exc
                self._stop.set()
                return


class MigrationExecutor:
    """Execute a confirmed plan with bounded, dependency-safe concurrency."""

    def __init__(
        self,
        store: CoreStore,
        drive: DriveService,
        wiki: WikiService,
        quota: DailyQuotaStore,
        *,
        max_workers: int = DEFAULT_MIGRATION_WORKERS,
    ) -> None:
        if max_workers < 1 or max_workers > 8:
            raise ValueError("migration worker count must be between 1 and 8")
        self.store = store
        self.drive = drive
        self.wiki = wiki
        self.quota = quota
        self.max_workers = int(max_workers)
        self._folder_token_cache: dict[tuple[str, str], str] = {}

    def execute(
        self,
        project_id: str,
        *,
        run_id: str | None = None,
        control: JobControl | None = None,
        progress: ProgressCallback | None = None,
    ) -> ExecutionResult:
        project = self.store.get_project(project_id)
        _assert_fixed_oauth_identity(project, self.drive)
        plan_id: str | None
        if run_id:
            durable_run = self.store.get_job_run(run_id)
            if durable_run.project_id != project_id:
                raise MigrationBlocked("运行记录与迁移项目不匹配")
            if durable_run.run_type != RunType.MIGRATION:
                raise MigrationBlocked("运行记录不是迁移任务")
            if not durable_run.plan_id:
                raise MigrationBlocked("运行记录缺少已确认的 plan_id")
            plan_id = str(durable_run.plan_id)
        else:
            durable_run = None
            plan_id = self.store.latest_plan_id(project_id)
        if not plan_id:
            raise MigrationBlocked("运行绑定的迁移计划不存在")
        guard = self.store.plan_execution_guard(project_id, plan_id)
        if guard["total"] <= 0:
            raise MigrationBlocked("运行绑定的迁移计划不存在")
        if guard["unconfirmed"]:
            raise MigrationBlocked("当前差异计划尚未最终确认")
        if guard["blocking"]:
            raise MigrationBlocked(f"计划包含 {guard['blocking']} 个冲突或人工处理项")
        if not project.target_space_id or not project.target_parent_node_token:
            raise MigrationBlocked("目标知识库尚未通过预检")

        if durable_run is None:
            durable_run = self.store.create_job_run(
                project_id,
                RunType.MIGRATION,
                status=RunStatus.RUNNING,
                scan_id=project.current_scan_id,
                plan_id=plan_id,
            )
        run_id = durable_run.id
        owner_id = f"{run_id}:{uuid.uuid4().hex}"
        lease = _ProjectLeaseHeartbeat(self.store, project_id, owner_id, run_id=run_id)
        lease.start()
        try:
            self.store.update_project(project_id, status=ProjectStatus.RUNNING)
            run_summary = self.store.plan_run_summary(project_id, plan_id)
            self.store.update_job_run(
                run_id,
                status=RunStatus.RUNNING,
                total_items=run_summary["total"],
                bytes_total=run_summary["bytes_total"],
                current_stage="DATA_MIGRATION",
                current_item="",
                last_message="迁移任务已启动",
                pause_requested=False,
                cancel_requested=False,
                heartbeat_at=utc_now(),
                started_at=durable_run.started_at or utc_now(),
                finished_at=None,
                error="",
                summary={"workers": self.max_workers, "in_flight": 0},
            )
            self.store.append_audit(
                project_id,
                "migration.started",
                "开始执行已确认的迁移计划",
                job_run_id=run_id,
                payload={"plan_id": plan_id, "total": run_summary["total"]},
            )
        except Exception:
            lease.close()
            raise

        controller = control or JobControl()
        api = getattr(self.drive, "api", None)
        interruptible = (
            api.interruptible(controller.wait)
            if api is not None and hasattr(api, "interruptible")
            else nullcontext()
        )
        interruptible.__enter__()
        counters = self.store.plan_execution_counters(project_id, plan_id)
        completed = counters["completed"]
        skipped = counters["skipped"]
        bytes_completed = counters["bytes_completed"]
        failed = conflicts = 0
        quota_paused = False
        total = run_summary["total"]
        after_order = -1
        last_progress_persist = 0.0

        try:
            with ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="folder2feishu-migrate",
            ) as workers:
                while True:
                    actions = self.store.list_plan_actions_batch(
                        project_id,
                        plan_id,
                        after_order=after_order,
                        limit=200,
                    )
                    if not actions:
                        break
                    items = self.store.get_inventory_items(
                        action.inventory_item_id for action in actions if action.inventory_item_id
                    )
                    cursor = 0
                    while cursor < len(actions):
                        controller.checkpoint()
                        lease.checkpoint()
                        group = self._parallel_action_group(actions, cursor)
                        cursor += len(group)
                        runnable: list[tuple[PlannedAction, InventoryItem | None]] = []
                        for action in group:
                            after_order = max(after_order, action.order_index)
                            item = (
                                items.get(action.inventory_item_id)
                                if action.inventory_item_id
                                else None
                            )
                            if action.state == MigrationState.DONE:
                                self._progress(
                                    progress,
                                    action,
                                    total,
                                    completed,
                                    failed,
                                    skipped,
                                )
                                continue
                            if action.action_type in {
                                ActionType.CONFLICT,
                                ActionType.MANUAL_ACTION,
                            }:
                                conflicts += 1
                                continue
                            runnable.append((action, item))

                        if not runnable:
                            continue

                        first_action = runnable[0][0]
                        last_index = max(action.order_index for action, _item in runnable)
                        self.store.update_job_run(
                            run_id,
                            current_stage=f"MIGRATING_{first_action.action_type.value}",
                            current_item=(
                                first_action.source_rel_path or first_action.previous_rel_path
                            ),
                            last_message=(
                                f"正在并行处理第 {first_action.order_index + 1:,}–"
                                f"{last_index + 1:,} / {total:,} 项"
                                if len(runnable) > 1
                                else f"正在处理第 {first_action.order_index + 1:,} / {total:,} 项"
                            ),
                            heartbeat_at=utc_now(),
                        )
                        for action, _item in runnable:
                            self._progress(
                                progress,
                                action,
                                total,
                                completed,
                                failed,
                                skipped,
                                current=True,
                            )

                        futures = {
                            workers.submit(
                                self._execute_action_safely,
                                project,
                                run_id,
                                action,
                                item,
                                controller,
                            ): action
                            for action, item in runnable
                        }
                        pending_count = len(futures)
                        for future in as_completed(futures):
                            outcome = future.result()
                            pending_count -= 1
                            if outcome.quota_paused:
                                quota_paused = True
                            if not outcome.already_done:
                                completed += int(outcome.completed)
                                skipped += int(outcome.skipped)
                                failed += outcome.failed
                                conflicts += outcome.conflicts
                                if outcome.completed:
                                    bytes_completed += outcome.item_bytes
                            lease.checkpoint()
                            now = time.monotonic()
                            if (
                                pending_count == 0
                                or quota_paused
                                or now - last_progress_persist >= RUN_PROGRESS_PERSIST_SECONDS
                            ):
                                self.store.update_job_run(
                                    run_id,
                                    completed_items=completed,
                                    failed_items=failed,
                                    skipped_items=skipped,
                                    bytes_completed=bytes_completed,
                                    current_item=(
                                        ""
                                        if pending_count == 0
                                        else outcome.action.source_rel_path
                                        or outcome.action.previous_rel_path
                                    ),
                                    last_message=(
                                        f"已处理 {completed + failed + conflicts:,} / {total:,} 项"
                                    ),
                                    heartbeat_at=utc_now(),
                                    summary={
                                        "conflicts": conflicts,
                                        "current_index": outcome.action.order_index,
                                        "workers": self.max_workers,
                                        "in_flight": pending_count,
                                    },
                                )
                                last_progress_persist = now
                            self._progress(
                                progress,
                                outcome.action,
                                total,
                                completed,
                                failed,
                                skipped,
                            )
                        if quota_paused:
                            break
                    if quota_paused:
                        break

            lease.checkpoint()
            status = (
                RunStatus.PAUSED
                if quota_paused
                else RunStatus.FAILED
                if failed or conflicts
                else RunStatus.COMPLETE
            )
            project_status = (
                ProjectStatus.PAUSED
                if quota_paused
                else ProjectStatus.BLOCKED
                if conflicts
                else ProjectStatus.COMPLETE
                if not failed
                else ProjectStatus.PLANNED
            )
            result = ExecutionResult(
                run_id=run_id,
                total=total,
                completed=completed,
                skipped=skipped,
                failed=failed,
                conflicts=conflicts,
                quota_paused=quota_paused,
            )
            self.store.update_job_run(
                run_id,
                status=status,
                completed_items=completed,
                failed_items=failed,
                skipped_items=skipped,
                summary={
                    "conflicts": conflicts,
                    "quota_paused": quota_paused,
                    "workers": self.max_workers,
                    "in_flight": 0,
                },
                bytes_completed=bytes_completed,
                current_stage="COMPLETED" if status == RunStatus.COMPLETE else "NEEDS_ATTENTION",
                current_item="",
                last_message=(
                    "迁移完成" if status == RunStatus.COMPLETE else "迁移结束，存在失败或冲突项目"
                ),
                heartbeat_at=utc_now(),
                finished_at=utc_now() if status != RunStatus.PAUSED else None,
            )
            self.store.update_project(project_id, status=project_status)
            self.store.append_audit(
                project_id,
                "migration.finished",
                "迁移运行已结束" if not quota_paused else "接口调用受限，已安全暂停",
                level=(
                    AuditLevel.WARNING if quota_paused or failed or conflicts else AuditLevel.INFO
                ),
                job_run_id=run_id,
                payload={
                    "completed": completed,
                    "skipped": skipped,
                    "failed": failed,
                    "conflicts": conflicts,
                    "quota_paused": quota_paused,
                },
            )
            return result
        except JobStopped:
            self.store.update_job_run(
                run_id,
                status=RunStatus.CANCELLED,
                completed_items=completed,
                failed_items=failed,
                skipped_items=skipped,
                bytes_completed=bytes_completed,
                current_stage="CANCELLED",
                current_item="",
                last_message="任务已由用户停止",
                cancel_requested=True,
                heartbeat_at=utc_now(),
                finished_at=utc_now(),
            )
            self.store.update_project(project_id, status=ProjectStatus.PAUSED)
            raise
        except Exception as exc:
            self.store.update_job_run(
                run_id,
                status=RunStatus.FAILED,
                completed_items=completed,
                failed_items=failed + 1,
                skipped_items=skipped,
                bytes_completed=bytes_completed,
                current_stage="FAILED",
                current_item="",
                last_message="迁移任务发生未预期错误",
                heartbeat_at=utc_now(),
                error=f"{type(exc).__name__}: {exc}",
                finished_at=utc_now(),
            )
            self.store.update_project(project_id, status=ProjectStatus.BLOCKED)
            raise
        finally:
            interruptible.__exit__(None, None, None)
            lease.close()

    def _parallel_action_group(
        self,
        actions: list[PlannedAction],
        start: int,
    ) -> list[PlannedAction]:
        """Return a bounded group whose members have no parent dependency.

        Folder actions are only concurrent at the same depth. Uploads are safe
        after all folder/move actions because the planner orders action types.
        Other actions stay serial until their dependency rules are explicitly
        proven safe.
        """

        first = actions[start]
        limit = self.max_workers * MAX_QUEUED_ACTIONS_PER_WORKER
        if first.action_type == ActionType.CREATE_FOLDER:
            first_depth = len(PurePosixPath(first.destination_rel_path).parts)

            def compatible(candidate: PlannedAction) -> bool:
                return (
                    candidate.action_type == ActionType.CREATE_FOLDER
                    and len(PurePosixPath(candidate.destination_rel_path).parts) == first_depth
                )

        elif first.action_type == ActionType.UPLOAD:

            def compatible(candidate: PlannedAction) -> bool:
                return candidate.action_type == ActionType.UPLOAD

        else:
            return [first]

        end = start + 1
        while end < len(actions) and end - start < limit and compatible(actions[end]):
            end += 1
        return actions[start:end]

    def _execute_action_safely(
        self,
        project: Project,
        run_id: str,
        action: PlannedAction,
        item: InventoryItem | None,
        controller: JobControl,
    ) -> _ActionOutcome:
        """Execute one independent action and durably classify expected failures."""

        api = getattr(self.drive, "api", None)
        interruptible = (
            api.interruptible(controller.wait)
            if api is not None and hasattr(api, "interruptible")
            else nullcontext()
        )
        item_bytes = int(item.size or 0) if item else 0
        with interruptible:
            controller.checkpoint()
            try:
                self._execute_action(project, action, item, index=action.order_index)
            except DailyQuotaExceeded as exc:
                self.store.update_plan_action(
                    action.id,
                    state=MigrationState.PAUSED,
                    merge_details={
                        "quota_reset_at": exc.reset_at.isoformat(),
                        "quota_used": exc.used,
                    },
                )
                self.store.append_audit(
                    project.id,
                    "migration.quota_paused",
                    str(exc),
                    level=AuditLevel.WARNING,
                    job_run_id=run_id,
                    planned_action_id=action.id,
                    rel_path=action.source_rel_path,
                    payload={"reset_at": exc.reset_at.isoformat()},
                )
                return _ActionOutcome(action, quota_paused=True)
            except MigrationBlocked as exc:
                self.store.update_plan_action(
                    action.id,
                    state=MigrationState.CONFLICT,
                    reason=str(exc),
                )
                self.store.append_audit(
                    project.id,
                    "migration.conflict",
                    str(exc),
                    level=AuditLevel.ERROR,
                    job_run_id=run_id,
                    planned_action_id=action.id,
                    rel_path=action.source_rel_path,
                )
                return _ActionOutcome(action, conflicts=1)
            except WikiMoveTaskFailedError as exc:
                state = MigrationState.RETRYABLE if exc.retryable else MigrationState.MANUAL_ACTION
                event_type = "migration.retryable" if exc.retryable else "migration.manual_action"
                self.store.update_plan_action(
                    action.id,
                    state=state,
                    reason=str(exc) if not exc.retryable else action.reason,
                    merge_details={
                        "last_error_type": type(exc).__name__,
                        "last_error": str(exc),
                    },
                )
                self.store.append_audit(
                    project.id,
                    event_type,
                    str(exc),
                    level=AuditLevel.ERROR,
                    job_run_id=run_id,
                    planned_action_id=action.id,
                    rel_path=action.source_rel_path,
                )
                return _ActionOutcome(
                    action,
                    failed=int(exc.retryable),
                    conflicts=int(not exc.retryable),
                )
            except (FeishuAmbiguousWriteError, FeishuError, OSError) as exc:
                self.store.update_plan_action(
                    action.id,
                    state=MigrationState.RETRYABLE,
                    merge_details={
                        "last_error_type": type(exc).__name__,
                        "last_error": str(exc),
                    },
                )
                self.store.append_audit(
                    project.id,
                    "migration.retryable",
                    str(exc),
                    level=AuditLevel.ERROR,
                    job_run_id=run_id,
                    planned_action_id=action.id,
                    rel_path=action.source_rel_path,
                )
                return _ActionOutcome(action, failed=1)
        return _ActionOutcome(
            action,
            item_bytes=item_bytes,
            completed=True,
            skipped=action.action_type in {ActionType.SKIP, ActionType.REPORT_MISSING},
        )

    def _execute_action(
        self,
        project: Project,
        action: PlannedAction,
        item: InventoryItem | None,
        *,
        index: int,
    ) -> None:
        if action.action_type in {ActionType.SKIP, ActionType.REPORT_MISSING}:
            self.store.update_plan_action(action.id, state=MigrationState.DONE)
            return
        if item is None:
            raise MigrationBlocked("计划项缺少本地盘点记录")
        if action.action_type == ActionType.CREATE_FOLDER:
            self._create_folder(project, action, item)
            return
        if action.action_type == ActionType.UPLOAD:
            file_token, wiki_token = self._upload_file(project, action, item, index=index)
            destination_parent = self._destination_parent(project, item.rel_path)
            self.store.complete_plan_action_with_mapping(
                action.id,
                wiki_node_token=wiki_token,
                object_token=file_token,
                mapping_values={
                    "project_id": project.id,
                    "inventory_item_id": item.id,
                    "item_kind": ItemKind.FILE,
                    "last_source_rel_path": item.rel_path,
                    "source_file_identity": item.file_identity,
                    "source_sha256": item.sha256,
                    "source_size": item.size,
                    "wiki_space_id": project.target_space_id,
                    "wiki_node_token": wiki_token,
                    "object_token": file_token,
                    "remote_parent_node_token": destination_parent,
                    "remote_title": item.name,
                    "remote_status": RemoteStatus.ACTIVE,
                    "last_verified_at": utc_now(),
                    "is_current": True,
                },
            )
            return
        if action.action_type in {ActionType.MOVE, ActionType.RENAME}:
            self._move_or_rename(project, action, item)
            return
        if action.action_type == ActionType.VERSION_UPDATE:
            self._version_update(project, action, item, index=index)
            return
        raise MigrationBlocked(f"不支持的计划动作：{action.action_type.value}")

    def _create_folder(self, project: Project, action: PlannedAction, item: InventoryItem) -> None:
        parent_token = self._destination_parent(project, item.rel_path)
        title = project.wrapper_name if item.rel_path == "" else item.name
        current, direct_create = self.store.prepare_folder_create_action(action.id)
        if current.wiki_node_token:
            # A prior process persisted the token before it stopped.  Resume
            # with a read-only reconciliation and never create a duplicate.
            result = self.wiki.reconcile_node(
                current.wiki_node_token,
                expected_space_id=project.target_space_id,
                expected_parent_token=parent_token,
                expected_title=title,
                expected_obj_token=current.object_token or None,
            )
            if result.status != ReconcileStatus.MATCH or not result.node:
                raise MigrationBlocked(
                    "已记录的目录节点与预期不一致：" + ",".join(result.differences)
                )
            node = result.node
        elif not direct_create:
            # The process may have stopped between POST and token persistence.
            # The existing reconciliation path is intentionally retained only
            # for this recovery case.
            node = self.wiki.ensure_docx_node(project.target_space_id, title, parent_token)
        else:
            # prepare_folder_create_action committed intent before this write.
            # A normal create now needs one POST; a restart remains recoverable
            # through the branch above.
            try:
                node = self.wiki.create_docx_node(
                    project.target_space_id,
                    title,
                    parent_token,
                )
            except FeishuAmbiguousWriteError:
                node = self.wiki.ensure_docx_node(
                    project.target_space_id,
                    title,
                    parent_token,
                )
        wiki_token = str(node["node_token"])
        object_token = str(node.get("obj_token") or "")
        # Persist action evidence and mapping in one transaction. If that
        # transaction fails, resume uses the pre-write intent to reconcile the
        # remote result before any replacement POST.
        self.store.complete_plan_action_with_mapping(
            action.id,
            wiki_node_token=wiki_token,
            object_token=object_token,
            mapping_values={
                "project_id": project.id,
                "inventory_item_id": item.id,
                "item_kind": ItemKind.FOLDER,
                "last_source_rel_path": item.rel_path,
                "source_file_identity": item.file_identity,
                "source_sha256": None,
                "source_size": None,
                "wiki_space_id": project.target_space_id,
                "wiki_node_token": wiki_token,
                "object_token": object_token,
                "remote_parent_node_token": parent_token,
                "remote_title": title,
                "remote_status": RemoteStatus.ACTIVE,
                "last_verified_at": utc_now(),
                "is_current": True,
            },
            merge_details={
                "wiki_node_token": wiki_token,
                "object_token": object_token,
            },
        )
        self._folder_token_cache[(project.id, item.rel_path)] = wiki_token

    def _upload_file(
        self,
        project: Project,
        action: PlannedAction,
        item: InventoryItem,
        *,
        index: int,
        wiki_parent_override: str | None = None,
    ) -> tuple[str, str]:
        source = self._verify_source_unchanged(project, item)
        parent_token = wiki_parent_override or self._destination_parent(project, item.rel_path)
        hooks = LedgerPersistenceHooks(
            self.store,
            project_id=project.id,
            planned_action_id=action.id,
            idempotency_key=item.id,
            upload_attempt=self._reserve_upload_attempt,
        )
        current = self.store.get_plan_action(action.id)
        file_token = current.drive_file_token
        wiki_token = current.wiki_node_token
        if wiki_token:
            result = self.wiki.reconcile_node(
                wiki_token,
                expected_space_id=project.target_space_id,
                expected_parent_token=parent_token,
                expected_title=item.name,
                expected_obj_token=file_token or None,
            )
            if result.status == ReconcileStatus.MATCH:
                return file_token, wiki_token
            if result.status == ReconcileStatus.CONFLICT:
                raise MigrationBlocked("已迁入节点被人工修改：" + ",".join(result.differences))
            # A missing node after a persisted token must never cause a blind upload.
            raise MigrationBlocked("已记录的知识库节点已不存在")

        if not file_token:
            assert item.size is not None
            staging = self.drive.ensure_staging(project.id, shard_index=index // 1_000)
            internal_name = deterministic_staging_name(project.id, item.id, item.name)
            self.store.update_plan_action(
                action.id,
                state=MigrationState.UPLOADING,
                merge_details={
                    "staging_parent_token": staging.shard_token,
                    "staging_name": internal_name,
                },
            )
            resume = hooks.resume_upload_session()
            staged = self.drive.stage_file(
                source,
                parent_node=staging.shard_token,
                project_id=project.id,
                item_key=item.id,
                original_name=item.name,
                resume_session=resume,
                hooks=hooks,
            )
            file_token = staged.file_token
            self.store.update_plan_action(
                action.id,
                state=MigrationState.DRIVE_UPLOADED,
                merge_details={"staging_name_restored": True},
            )
        elif not bool((current.details or {}).get("staging_name_restored")):
            # on_file_token is a durability boundary that intentionally runs
            # before Drive restores the deterministic staging title. A crash
            # in that small window resumes here and idempotently restores the
            # original title before any move-to-Wiki request is issued.
            self.drive.rename_file(file_token, item.name, object_type="file")
            self.store.update_plan_action(
                action.id,
                state=MigrationState.DRIVE_UPLOADED,
                merge_details={"staging_name_restored": True},
            )

        current = self.store.get_plan_action(action.id)
        self.store.update_plan_action(action.id, state=MigrationState.WIKI_MOVING)
        wiki_token = self.wiki.move_file_to_wiki(
            project.target_space_id,
            file_token=file_token,
            parent_wiki_token=parent_token,
            hooks=hooks,
            existing_task_id=current.move_task_id or None,
        )
        self.store.update_plan_action(action.id, state=MigrationState.VERIFYING)
        result = self.wiki.reconcile_node(
            wiki_token,
            expected_space_id=project.target_space_id,
            expected_parent_token=parent_token,
            expected_title=item.name,
            expected_obj_token=file_token,
        )
        if result.status != ReconcileStatus.MATCH:
            raise MigrationBlocked("文件迁入后远端对账不一致：" + ",".join(result.differences))
        return file_token, wiki_token

    def _reserve_upload_attempt(self) -> None:
        # Called by FeishuAPIClient immediately before every actual upload HTTP
        # attempt, including controlled 429/1061045 and multipart retries.
        self.quota.reserve(1)

    def _move_or_rename(self, project: Project, action: PlannedAction, item: InventoryItem) -> None:
        mapping = self._mapping_for_action(action)
        destination_parent = self._destination_parent(project, item.rel_path)
        self.store.update_plan_action(
            action.id,
            merge_details={
                "intended_parent_token": destination_parent,
                "intended_title": item.name,
            },
        )
        intended = self.wiki.reconcile_node(
            mapping.wiki_node_token,
            expected_space_id=project.target_space_id,
            expected_parent_token=destination_parent,
            expected_title=item.name,
            expected_obj_token=mapping.object_token or None,
        )
        already_applied = intended.status == ReconcileStatus.MATCH
        if not already_applied:
            self._assert_mapping_unchanged(mapping)

        if not already_applied and mapping.remote_parent_node_token != destination_parent:
            try:
                self.wiki.move_node(
                    project.target_space_id,
                    mapping.wiki_node_token,
                    target_parent_token=destination_parent,
                )
            except FeishuAmbiguousWriteError:
                moved = self.wiki.reconcile_node(
                    mapping.wiki_node_token,
                    expected_space_id=project.target_space_id,
                    expected_parent_token=destination_parent,
                    expected_title=mapping.remote_title,
                    expected_obj_token=mapping.object_token or None,
                )
                if moved.status != ReconcileStatus.MATCH:
                    raise

        if not already_applied and mapping.remote_title != item.name:
            try:
                if item.kind == ItemKind.FILE:
                    if not mapping.object_token:
                        raise MigrationBlocked("原格式文件映射缺少 Drive 对象 token")
                    self.drive.rename_file(mapping.object_token, item.name, object_type="file")
                else:
                    self.wiki.rename_node(
                        project.target_space_id,
                        mapping.wiki_node_token,
                        item.name,
                    )
            except FeishuAmbiguousWriteError:
                renamed = self.wiki.reconcile_node(
                    mapping.wiki_node_token,
                    expected_space_id=project.target_space_id,
                    expected_parent_token=destination_parent,
                    expected_title=item.name,
                    expected_obj_token=mapping.object_token or None,
                )
                if renamed.status != ReconcileStatus.MATCH:
                    raise

        result = self.wiki.reconcile_node(
            mapping.wiki_node_token,
            expected_space_id=project.target_space_id,
            expected_parent_token=destination_parent,
            expected_title=item.name,
            expected_obj_token=mapping.object_token or None,
        )
        if result.status != ReconcileStatus.MATCH:
            raise MigrationBlocked("移动或改名后的远端对账失败：" + ",".join(result.differences))
        self.store.upsert_remote_mapping(
            id=mapping.id,
            project_id=project.id,
            inventory_item_id=item.id,
            item_kind=item.kind,
            last_source_rel_path=item.rel_path,
            source_file_identity=item.file_identity,
            source_sha256=item.sha256,
            source_size=item.size,
            wiki_space_id=project.target_space_id,
            wiki_node_token=mapping.wiki_node_token,
            object_token=mapping.object_token,
            remote_parent_node_token=destination_parent,
            remote_title=item.name,
            remote_status=RemoteStatus.ACTIVE,
            conflict_reason="",
            last_verified_at=utc_now(),
            is_current=True,
        )
        self.store.update_plan_action(action.id, state=MigrationState.DONE)

    def _version_update(
        self,
        project: Project,
        action: PlannedAction,
        item: InventoryItem,
        *,
        index: int,
    ) -> None:
        old = self._mapping_for_action(action)
        wrapper = self.store.find_current_remote_mapping(project.id, rel_path="")
        if wrapper is None:
            raise MigrationBlocked("找不到项目根目录知识库节点")
        destination_parent = self._destination_parent(project, item.rel_path)

        # A new version first enters a dedicated Wiki holding node.  Only after
        # it is verified do we archive the old node and move the new node into place.
        holding = self.wiki.ensure_docx_node(
            project.target_space_id,
            "_Folder2Feishu_换版暂存",
            wrapper.wiki_node_token,
        )
        holding_token = str(holding["node_token"])
        current = self.store.get_plan_action(action.id)
        details = current.details or {}
        new_already_at_destination = False
        if current.wiki_node_token and current.drive_file_token:
            at_destination = self.wiki.reconcile_node(
                current.wiki_node_token,
                expected_space_id=project.target_space_id,
                expected_parent_token=destination_parent,
                expected_title=item.name,
                expected_obj_token=current.drive_file_token,
            )
            new_already_at_destination = at_destination.status == ReconcileStatus.MATCH
        if new_already_at_destination:
            file_token = current.drive_file_token
            new_wiki_token = current.wiki_node_token
        else:
            file_token, new_wiki_token = self._upload_file(
                project,
                action,
                item,
                index=index,
                wiki_parent_override=holding_token,
            )

        if not details.get("old_archived"):
            history_parent = str(details.get("history_parent_token") or "")
            if not history_parent:
                history_parent = self._ensure_history_parent(project, item, wrapper)
                self.store.update_plan_action(
                    action.id,
                    merge_details={"history_parent_token": history_parent},
                )
            archived = self.wiki.reconcile_node(
                old.wiki_node_token,
                expected_space_id=project.target_space_id,
                expected_parent_token=history_parent,
                expected_title=old.remote_title,
                expected_obj_token=old.object_token or None,
            )
            if archived.status != ReconcileStatus.MATCH:
                self._assert_mapping_unchanged(old)
                try:
                    self.wiki.archive_file_node(
                        project.target_space_id,
                        old.wiki_node_token,
                        history_parent_token=history_parent,
                    )
                except FeishuAmbiguousWriteError:
                    archived = self.wiki.reconcile_node(
                        old.wiki_node_token,
                        expected_space_id=project.target_space_id,
                        expected_parent_token=history_parent,
                        expected_title=old.remote_title,
                        expected_obj_token=old.object_token or None,
                    )
                    if archived.status != ReconcileStatus.MATCH:
                        raise
            self.store.update_plan_action(
                action.id,
                merge_details={
                    "old_archived": True,
                    "history_parent_token": history_parent,
                },
            )

        if not new_already_at_destination:
            try:
                self.wiki.move_node(
                    project.target_space_id,
                    new_wiki_token,
                    target_parent_token=destination_parent,
                )
            except FeishuAmbiguousWriteError:
                moved = self.wiki.reconcile_node(
                    new_wiki_token,
                    expected_space_id=project.target_space_id,
                    expected_parent_token=destination_parent,
                    expected_title=item.name,
                    expected_obj_token=file_token,
                )
                if moved.status != ReconcileStatus.MATCH:
                    # Keep both versions in Wiki. The old version is recoverable
                    # from history and the new one remains in the holding node.
                    raise
        result = self.wiki.reconcile_node(
            new_wiki_token,
            expected_space_id=project.target_space_id,
            expected_parent_token=destination_parent,
            expected_title=item.name,
            expected_obj_token=file_token,
        )
        if result.status != ReconcileStatus.MATCH:
            raise MigrationBlocked("新版本进入目标位置后对账失败：" + ",".join(result.differences))

        self.store.mark_remote_mapping_historical(old.id)
        self.store.upsert_remote_mapping(
            project_id=project.id,
            inventory_item_id=item.id,
            item_kind=ItemKind.FILE,
            last_source_rel_path=item.rel_path,
            source_file_identity=item.file_identity,
            source_sha256=item.sha256,
            source_size=item.size,
            wiki_space_id=project.target_space_id,
            wiki_node_token=new_wiki_token,
            object_token=file_token,
            remote_parent_node_token=destination_parent,
            remote_title=item.name,
            remote_status=RemoteStatus.ACTIVE,
            last_verified_at=utc_now(),
            is_current=True,
        )
        self.store.update_plan_action(action.id, state=MigrationState.DONE)

    def _ensure_history_parent(
        self, project: Project, item: InventoryItem, wrapper: RemoteMapping
    ) -> str:
        parent = str(
            self.wiki.ensure_docx_node(
                project.target_space_id,
                "_Folder2Feishu_历史版本",
                wrapper.wiki_node_token,
            )["node_token"]
        )
        source_parent = _parent_rel_path(item.rel_path) or ""
        for part in PurePosixPath(source_parent).parts:
            parent = str(
                self.wiki.ensure_docx_node(project.target_space_id, part, parent)["node_token"]
            )
        timestamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        return str(
            self.wiki.ensure_docx_node(project.target_space_id, timestamp, parent)["node_token"]
        )

    def _verify_source_unchanged(self, project: Project, item: InventoryItem) -> Path:
        source = _safe_source_path(project, item)
        try:
            stat_result = source.stat()
        except OSError as exc:
            raise MigrationBlocked(f"源文件已不可读：{item.rel_path}") from exc
        if not source.is_file() or stat_result.st_size != item.size:
            raise MigrationBlocked(f"源文件在计划后发生变化：{item.rel_path}")
        digest = sha256_file(source)
        if digest != item.sha256:
            raise MigrationBlocked(f"源文件在计划后发生变化：{item.rel_path}")
        return source

    def _destination_parent(self, project: Project, rel_path: str) -> str:
        parent_rel = _parent_rel_path(rel_path)
        if parent_rel is None:
            return project.target_parent_node_token
        cache_key = (project.id, parent_rel)
        cached = self._folder_token_cache.get(cache_key)
        if cached:
            return cached
        mapping = self.store.find_current_remote_mapping(project.id, rel_path=parent_rel)
        if mapping is None or mapping.item_kind != ItemKind.FOLDER:
            raise MigrationBlocked(f"目标父目录尚未建立：{parent_rel or project.wrapper_name}")
        if mapping.remote_status != RemoteStatus.ACTIVE:
            raise MigrationBlocked("目标父目录远端状态异常")
        self._folder_token_cache[cache_key] = mapping.wiki_node_token
        return mapping.wiki_node_token

    def _mapping_for_action(self, action: PlannedAction) -> RemoteMapping:
        if not action.remote_mapping_id:
            raise MigrationBlocked("计划动作缺少远端映射")
        return self.store.get_remote_mapping(action.remote_mapping_id)

    def _assert_mapping_unchanged(self, mapping: RemoteMapping) -> None:
        result = self.wiki.reconcile_node(
            mapping.wiki_node_token,
            expected_space_id=mapping.wiki_space_id,
            expected_parent_token=mapping.remote_parent_node_token,
            expected_title=mapping.remote_title,
            expected_obj_token=mapping.object_token or None,
        )
        if result.status == ReconcileStatus.MISSING:
            self.store.upsert_remote_mapping(
                id=mapping.id,
                remote_status=RemoteStatus.MISSING,
                conflict_reason="飞书节点已被人工删除",
            )
            raise MigrationBlocked("飞书节点已被人工删除")
        if result.status == ReconcileStatus.CONFLICT:
            self.store.upsert_remote_mapping(
                id=mapping.id,
                remote_status=RemoteStatus.CONFLICT,
                conflict_reason="飞书节点已被人工移动或改名",
            )
            raise MigrationBlocked("飞书节点已被人工修改：" + ",".join(result.differences))
        self.store.upsert_remote_mapping(
            id=mapping.id,
            remote_status=RemoteStatus.ACTIVE,
            conflict_reason="",
            last_verified_at=utc_now(),
        )

    @staticmethod
    def _progress(
        callback: ProgressCallback | None,
        action: PlannedAction,
        total: int,
        completed: int,
        failed: int,
        skipped: int,
        *,
        current: bool = False,
    ) -> None:
        if callback:
            callback(
                total=total,
                completed=completed,
                failed=failed,
                skipped=skipped,
                current_item=action.source_rel_path if current else "",
            )


class RemoteReconciler:
    """Read back every current Wiki mapping and mark manual drift as conflict."""

    def __init__(
        self,
        store: CoreStore,
        drive: DriveService,
        wiki: WikiService,
    ) -> None:
        self.store = store
        self.drive = drive
        self.wiki = wiki

    def reconcile(
        self,
        project_id: str,
        *,
        run_id: str | None = None,
        control: JobControl | None = None,
        progress: ProgressCallback | None = None,
    ) -> ReconcileSummary:
        project = self.store.get_project(project_id)
        _assert_fixed_oauth_identity(project, self.drive)
        controller = control or JobControl()
        owner_id = f"reconcile:{uuid.uuid4().hex}"
        lease = _ProjectLeaseHeartbeat(
            self.store,
            project_id,
            owner_id,
            run_id=run_id,
        )
        lease.start()
        run = None
        try:
            total = self.store.count_remote_mappings(project_id, current_only=True)
            matched = missing = conflicts = 0
            if run_id:
                run = self.store.get_job_run(run_id)
                self.store.update_job_run(
                    run.id,
                    status=RunStatus.RUNNING,
                    total_items=total,
                    current_stage="REMOTE_RECONCILIATION",
                    last_message="正在回读飞书知识库节点",
                    started_at=run.started_at or utc_now(),
                    heartbeat_at=utc_now(),
                    finished_at=None,
                )
            else:
                run = self.store.create_job_run(
                    project_id,
                    RunType.RECONCILIATION,
                    status=RunStatus.RUNNING,
                    total_items=total,
                    current_stage="REMOTE_RECONCILIATION",
                )
            processed = 0
            after_id = ""
            while True:
                mappings = self.store.list_remote_mappings_batch(
                    project_id,
                    after_id=after_id,
                    current_only=True,
                    limit=200,
                )
                if not mappings:
                    break
                for mapping in mappings:
                    after_id = mapping.id
                    controller.checkpoint()
                    lease.checkpoint()
                    self.store.update_job_run(
                        run.id,
                        current_item=mapping.last_source_rel_path,
                        last_message=f"正在校验第 {processed + 1:,} / {total:,} 个节点",
                        heartbeat_at=utc_now(),
                    )
                    result = self.wiki.reconcile_node(
                        mapping.wiki_node_token,
                        expected_space_id=mapping.wiki_space_id,
                        expected_parent_token=mapping.remote_parent_node_token,
                        expected_title=mapping.remote_title,
                        expected_obj_token=mapping.object_token or None,
                    )
                    if result.status == ReconcileStatus.MATCH:
                        matched += 1
                        status = RemoteStatus.ACTIVE
                        reason = ""
                    elif result.status == ReconcileStatus.MISSING:
                        missing += 1
                        status = RemoteStatus.MISSING
                        reason = "飞书节点不存在"
                    else:
                        conflicts += 1
                        status = RemoteStatus.CONFLICT
                        reason = "远端差异：" + ",".join(result.differences)
                    self.store.upsert_remote_mapping(
                        id=mapping.id,
                        remote_status=status,
                        conflict_reason=reason,
                        last_verified_at=utc_now(),
                    )
                    processed += 1
                    self.store.update_job_run(
                        run.id,
                        completed_items=processed,
                        current_item="",
                        heartbeat_at=utc_now(),
                        summary={
                            "matched": matched,
                            "missing": missing,
                            "conflicts": conflicts,
                        },
                    )
                    if progress:
                        progress(
                            total=total,
                            completed=processed,
                            failed=missing + conflicts,
                            current_item="",
                        )
            lease.checkpoint()
            summary = ReconcileSummary(
                checked=processed,
                matched=matched,
                missing=missing,
                conflicts=conflicts,
            )
            self.store.update_job_run(
                run.id,
                status=RunStatus.COMPLETE,
                total_items=total,
                completed_items=processed,
                summary={
                    "matched": matched,
                    "missing": missing,
                    "conflicts": conflicts,
                },
                current_stage="COMPLETED",
                current_item="",
                last_message="远端对账完成",
                heartbeat_at=utc_now(),
                finished_at=utc_now(),
            )
            self.store.append_audit(
                project_id,
                "reconcile.finished",
                "远端映射回读完成",
                level=AuditLevel.WARNING if missing or conflicts else AuditLevel.INFO,
                job_run_id=run.id,
                payload={
                    "checked": processed,
                    "matched": matched,
                    "missing": missing,
                    "conflicts": conflicts,
                },
            )
            return summary
        except JobStopped:
            if run is not None:
                self.store.update_job_run(
                    run.id,
                    status=RunStatus.CANCELLED,
                    current_stage="CANCELLED",
                    current_item="",
                    last_message="远端对账已由用户停止",
                    cancel_requested=True,
                    heartbeat_at=utc_now(),
                    finished_at=utc_now(),
                )
            raise
        except Exception as exc:
            if run is not None:
                self.store.update_job_run(
                    run.id,
                    status=RunStatus.FAILED,
                    current_stage="FAILED",
                    current_item="",
                    last_message="远端对账失败",
                    heartbeat_at=utc_now(),
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=utc_now(),
                )
            raise
        finally:
            lease.close()
