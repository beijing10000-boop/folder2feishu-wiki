"""Read-only, resumable-friendly local inventory scanner."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .database import CoreStore
from .enums import (
    AuditLevel,
    InventoryState,
    IssueCode,
    IssueSeverity,
    ItemKind,
    ProjectStatus,
    RunStatus,
    RunType,
)
from .models import utc_now

FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
MAX_FEISHU_NAME_LENGTH = 250
MAX_WIKI_LOCAL_DEPTH = 49
MAX_WIKI_CHILDREN = 2_000
HASH_BLOCK_SIZE = 4 * 1024 * 1024
ONEDRIVE_INTERNAL_NAMES = frozenset({".849c9593-d756-4e56-8d6e-42412f2a707b"})


class ScanCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScanProgress:
    scan_id: str
    scanned_items: int
    folders: int
    files: int
    bytes: int
    issues: int
    current_rel_path: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    project_id: str
    scan_id: str
    run_id: str
    source_root: str
    complete: bool
    cancelled: bool
    folders: int
    files: int
    bytes: int
    issues: int
    blocking_issues: int
    operational_errors: int


@dataclass(frozen=True, slots=True)
class _CachedDigest:
    file_identity: str
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _FileCandidate:
    path: Path
    rel_path: str
    parent_rel_path: str
    name: str
    depth: int
    stat_result: os.stat_result
    file_attributes: int
    is_offline: bool
    is_recall_on_open: bool
    is_recall_on_data_access: bool
    file_identity: str | None
    cached_sha256: str | None


def portable_rel(path: Path, root: Path) -> str:
    if path == root:
        return ""
    return "/".join(path.relative_to(root).parts)


def parent_rel(rel_path: str) -> str | None:
    if rel_path == "":
        return None
    return rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""


@lru_cache(maxsize=1)
def _windows_identity_api() -> tuple[Any, Any, Any, Any, Any]:
    """Initialize the Win32 metadata API once, not once per scanned file."""

    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    return (
        ctypes,
        ByHandleFileInformation,
        create_file,
        get_information,
        close_handle,
    )


def _windows_file_identity(path: Path) -> str | None:
    """Read Windows volume/file indexes using metadata-only handle access."""

    if os.name != "nt":
        return None
    ctypes, information_type, create_file, get_information, close_handle = _windows_identity_api()
    from ctypes import wintypes

    share_all = 0x1 | 0x2 | 0x4
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    handle = create_file(
        str(path),
        0,  # metadata access only
        share_all,
        None,
        open_existing,
        backup_semantics | open_reparse_point,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        return None
    try:
        information = information_type()
        if not get_information(handle, ctypes.byref(information)):
            return None
        index = (information.file_index_high << 32) | information.file_index_low
        if not index:
            return None
        return f"{information.volume_serial_number:08x}:{index:016x}"
    finally:
        close_handle(handle)


def file_identity(stat_result: os.stat_result, path: Path | None = None) -> str | None:
    """Return a volume/inode identity without opening or changing the file."""

    device = int(getattr(stat_result, "st_dev", 0) or 0)
    inode = int(getattr(stat_result, "st_ino", 0) or 0)
    if inode:
        return f"{device:x}:{inode:x}"
    return _windows_file_identity(path) if path is not None else None


def file_attribute_flags(attributes: int) -> tuple[bool, bool, bool]:
    offline = bool(attributes & FILE_ATTRIBUTE_OFFLINE)
    recall_open = bool(attributes & FILE_ATTRIBUTE_RECALL_ON_OPEN)
    recall_data = bool(attributes & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    return offline, recall_open, recall_data


def sha256_file(
    path: Path,
    *,
    cancel_check: Callable[[], bool] | None = None,
    block_size: int = HASH_BLOCK_SIZE,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_size):
            if cancel_check and cancel_check():
                raise ScanCancelled("scan cancelled while hashing")
            digest.update(block)
    return digest.hexdigest()


def _error_details(exc: OSError, path: Path) -> dict[str, Any]:
    return {
        "exception": type(exc).__name__,
        "errno": exc.errno,
        "winerror": getattr(exc, "winerror", None),
        "path": str(path),
    }


def _is_cancelled(cancel_check: Callable[[], bool] | Any | None) -> bool:
    if cancel_check is None:
        return False
    if callable(cancel_check):
        return bool(cancel_check())
    is_set = getattr(cancel_check, "is_set", None)
    return bool(is_set and is_set())


class InventoryScanner:
    """Inventory a local tree without following links or hydrating OneDrive.

    Facts are committed in bounded batches.  A cancelled or failed scan remains
    auditable, but ``project.scan_complete`` is false so a migration plan cannot
    accidentally be built from a partial tree.
    """

    def __init__(
        self,
        store: CoreStore,
        *,
        batch_size: int = 1_000,
        progress_interval: int = 100,
        hash_workers: int = 8,
        hash_queue_size: int | None = None,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if hash_workers < 1 or hash_workers > 16:
            raise ValueError("hash_workers must be between 1 and 16")
        self.store = store
        self.batch_size = batch_size
        self.progress_interval = max(1, progress_interval)
        self.hash_workers = hash_workers
        self.hash_queue_size = max(hash_workers, hash_queue_size or hash_workers * 8)

    def _digest_caches(
        self,
        project_id: str,
    ) -> tuple[dict[str, _CachedDigest], dict[str, _CachedDigest]]:
        """Load only reusable digests from the previous inventory.

        A hash is reusable only when the stable file identity, size, and
        nanosecond modification time all still match. Duplicate identities
        (for example hard links or stale moved rows) are never used for
        identity-only matching.
        """

        by_path: dict[str, _CachedDigest] = {}
        by_identity: dict[str, _CachedDigest] = {}
        duplicate_identities: set[str] = set()
        for item in self.store.list_inventory(project_id, kind=ItemKind.FILE):
            if (
                not item.sha256
                or not item.file_identity
                or item.size is None
                or item.mtime_ns is None
            ):
                continue
            cached = _CachedDigest(
                file_identity=item.file_identity,
                size=int(item.size),
                mtime_ns=int(item.mtime_ns),
                sha256=item.sha256,
            )
            by_path[item.rel_path] = cached
            if item.file_identity in by_identity:
                duplicate_identities.add(item.file_identity)
            else:
                by_identity[item.file_identity] = cached
        for identity in duplicate_identities:
            by_identity.pop(identity, None)
        return by_path, by_identity

    @staticmethod
    def _cached_digest(
        rel_path: str,
        *,
        file_identity_value: str | None,
        size: int,
        mtime_ns: int,
        by_path: dict[str, _CachedDigest],
        by_identity: dict[str, _CachedDigest],
    ) -> str | None:
        if not file_identity_value:
            return None
        candidates = (
            by_path.get(rel_path),
            by_identity.get(file_identity_value),
        )
        for cached in candidates:
            if (
                cached
                and cached.file_identity == file_identity_value
                and cached.size == size
                and cached.mtime_ns == mtime_ns
            ):
                return cached.sha256
        return None

    def scan(
        self,
        project_id: str,
        source_root: str | Path | None = None,
        *,
        run_id: str | None = None,
        cancel: Callable[[], bool] | Any | None = None,
        progress: Callable[[ScanProgress], None] | None = None,
    ) -> ScanResult:
        project = self.store.get_project(project_id)
        root = Path(source_root or project.source_root).expanduser().absolute()
        digest_by_path, digest_by_identity = self._digest_caches(project_id)
        scan_id = uuid.uuid4().hex
        if run_id:
            run = self.store.get_job_run(run_id)
            self.store.update_job_run(
                run.id,
                status=RunStatus.RUNNING,
                scan_id=scan_id,
                current_stage="SCANNING",
                current_item="",
                last_message="正在读取本地目录并计算文件指纹",
                heartbeat_at=utc_now(),
                started_at=run.started_at or utc_now(),
                finished_at=None,
                error="",
            )
        else:
            run = self.store.create_job_run(
                project_id,
                RunType.SCAN,
                status=RunStatus.RUNNING,
                scan_id=scan_id,
                current_stage="SCANNING",
            )
        self.store.update_project(
            project_id,
            source_root=str(root),
            current_scan_id=scan_id,
            scan_complete=False,
        )
        self.store.replace_inventory(project_id, scan_id, (), mark_absent=True)

        counts = {
            "folders": 0,
            "files": 0,
            "bytes": 0,
            "issues": 0,
            "blocking_issues": 0,
            "operational_errors": 0,
        }
        item_batch: list[dict[str, Any]] = []
        issue_batch: list[dict[str, Any]] = []
        cancelled = False
        unexpected_error: Exception | None = None
        hash_stats = {
            "hashes_computed": 0,
            "hashes_reused": 0,
        }
        hash_executor = ThreadPoolExecutor(
            max_workers=self.hash_workers,
            thread_name_prefix="folder2feishu-hash",
        )
        pending_hashes: deque[tuple[_FileCandidate, Future[str]]] = deque()
        last_persist_at = time.monotonic()

        def flush() -> None:
            if item_batch:
                self.store.replace_inventory(project_id, scan_id, item_batch, mark_absent=False)
                item_batch.clear()
            if issue_batch:
                self.store.add_scan_issues(project_id, scan_id, issue_batch)
                issue_batch.clear()

        def add_issue(
            code: IssueCode,
            severity: IssueSeverity,
            message: str,
            rel_path: str,
            *,
            details: dict[str, Any] | None = None,
            operational: bool = False,
        ) -> None:
            issue_batch.append(
                {
                    "code": code,
                    "severity": severity,
                    "message": message,
                    "rel_path": rel_path,
                    "details": details or {},
                }
            )
            counts["issues"] += 1
            counts["blocking_issues"] += int(severity == IssueSeverity.BLOCKING)
            counts["operational_errors"] += int(operational)
            if len(issue_batch) >= self.batch_size:
                flush()

        def emit_progress(current: str, *, force: bool = False) -> None:
            nonlocal last_persist_at
            scanned = counts["folders"] + counts["files"]
            if progress and (force or scanned % self.progress_interval == 0):
                progress(
                    ScanProgress(
                        scan_id=scan_id,
                        scanned_items=scanned,
                        folders=counts["folders"],
                        files=counts["files"],
                        bytes=counts["bytes"],
                        issues=counts["issues"],
                        current_rel_path=current,
                    )
                )
            now_monotonic = time.monotonic()
            should_persist = (
                force
                or scanned % max(1_000, self.progress_interval) == 0
                or now_monotonic - last_persist_at >= 1.0
            )
            if should_persist:
                self.store.update_job_run(
                    run.id,
                    total_items=scanned,
                    completed_items=scanned,
                    bytes_completed=counts["bytes"],
                    current_stage="SCANNING",
                    current_item=current,
                    last_message=f"已盘点 {scanned:,} 项",
                    heartbeat_at=utc_now(),
                    summary={
                        "folders": counts["folders"],
                        "files": counts["files"],
                        "issues": counts["issues"],
                    },
                )
                last_persist_at = now_monotonic

        def check_cancel() -> None:
            if _is_cancelled(cancel):
                raise ScanCancelled("scan cancelled")

        def record_file(candidate: _FileCandidate, digest: str | None) -> None:
            size = int(candidate.stat_result.st_size)
            manual = (
                candidate.is_offline
                or candidate.is_recall_on_open
                or candidate.is_recall_on_data_access
                or digest is None
                or len(candidate.name) > MAX_FEISHU_NAME_LENGTH
                or candidate.depth > MAX_WIKI_LOCAL_DEPTH
            )
            item_batch.append(
                {
                    "kind": ItemKind.FILE,
                    "rel_path": candidate.rel_path,
                    "parent_rel_path": candidate.parent_rel_path,
                    "name": candidate.name,
                    "depth": candidate.depth,
                    "file_identity": candidate.file_identity,
                    "size": size,
                    "mtime_ns": int(candidate.stat_result.st_mtime_ns),
                    "sha256": digest,
                    "file_attributes": candidate.file_attributes,
                    "is_offline": candidate.is_offline,
                    "is_recall_on_open": candidate.is_recall_on_open,
                    "is_recall_on_data_access": candidate.is_recall_on_data_access,
                    "state": (
                        InventoryState.MANUAL_ACTION if manual else InventoryState.DISCOVERED
                    ),
                }
            )
            counts["files"] += 1
            counts["bytes"] += size
            self._add_preflight_issues(
                add_issue,
                name=candidate.name,
                rel_path=candidate.rel_path,
                depth=candidate.depth,
                is_file=True,
                size=size,
                offline=candidate.is_offline,
                recall_open=candidate.is_recall_on_open,
                recall_data=candidate.is_recall_on_data_access,
            )
            if len(item_batch) >= self.batch_size:
                flush()
            emit_progress(candidate.rel_path)

        def finish_hash(candidate: _FileCandidate, future: Future[str]) -> None:
            digest: str | None = None
            try:
                digest = future.result()
                hash_stats["hashes_computed"] += 1
            except ScanCancelled:
                raise
            except OSError as exc:
                add_issue(
                    IssueCode.HASH_ERROR,
                    IssueSeverity.BLOCKING,
                    f"cannot hash file: {exc}",
                    candidate.rel_path,
                    details=_error_details(exc, candidate.path),
                    operational=True,
                )
            record_file(candidate, digest)

        def drain_hashes(*, force: bool = False) -> None:
            while pending_hashes and (force or len(pending_hashes) >= self.hash_queue_size):
                check_cancel()
                candidate, future = pending_hashes.popleft()
                finish_hash(candidate, future)

        def queue_file(candidate: _FileCandidate) -> None:
            if candidate.cached_sha256 is not None:
                hash_stats["hashes_reused"] += 1
                record_file(candidate, candidate.cached_sha256)
                return
            if (
                candidate.is_offline
                or candidate.is_recall_on_open
                or candidate.is_recall_on_data_access
            ):
                record_file(candidate, None)
                return
            pending_hashes.append(
                (
                    candidate,
                    hash_executor.submit(
                        sha256_file,
                        candidate.path,
                        cancel_check=lambda: _is_cancelled(cancel),
                    ),
                )
            )
            drain_hashes()

        try:
            if not root.is_dir():
                add_issue(
                    IssueCode.SOURCE_ROOT_UNAVAILABLE,
                    IssueSeverity.BLOCKING,
                    f"source root is not an accessible directory: {root}",
                    "",
                    details={"path": str(root)},
                    operational=True,
                )
            else:
                stack: list[tuple[Path, str, int]] = [(root, "", 0)]
                while stack:
                    check_cancel()
                    directory, rel_path, depth = stack.pop()
                    try:
                        directory_stat = directory.stat(follow_symlinks=False)
                    except OSError as exc:
                        add_issue(
                            IssueCode.STAT_ERROR,
                            IssueSeverity.BLOCKING,
                            f"cannot read directory metadata: {exc}",
                            rel_path,
                            details=_error_details(exc, directory),
                            operational=True,
                        )
                        continue

                    attrs = int(getattr(directory_stat, "st_file_attributes", 0) or 0)
                    offline, recall_open, recall_data = file_attribute_flags(attrs)
                    manual = (
                        offline
                        or recall_open
                        or recall_data
                        or len(directory.name) > MAX_FEISHU_NAME_LENGTH
                        or depth > MAX_WIKI_LOCAL_DEPTH
                    )
                    item_batch.append(
                        {
                            "kind": ItemKind.FOLDER,
                            "rel_path": rel_path,
                            "parent_rel_path": parent_rel(rel_path),
                            "name": directory.name,
                            "depth": depth,
                            "file_identity": (
                                None
                                if offline or recall_open or recall_data
                                else file_identity(directory_stat, directory)
                            ),
                            "size": None,
                            "mtime_ns": int(directory_stat.st_mtime_ns),
                            "sha256": None,
                            "file_attributes": attrs,
                            "is_offline": offline,
                            "is_recall_on_open": recall_open,
                            "is_recall_on_data_access": recall_data,
                            "state": (
                                InventoryState.MANUAL_ACTION
                                if manual
                                else InventoryState.DISCOVERED
                            ),
                        }
                    )
                    counts["folders"] += 1
                    self._add_preflight_issues(
                        add_issue,
                        name=directory.name,
                        rel_path=rel_path,
                        depth=depth,
                        is_file=False,
                        size=None,
                        offline=offline,
                        recall_open=recall_open,
                        recall_data=recall_data,
                    )

                    # Opening a recall/offline directory may hydrate content.
                    if offline or recall_open or recall_data:
                        emit_progress(rel_path)
                        continue
                    try:
                        with os.scandir(directory) as iterator:
                            entries = sorted(
                                (
                                    entry
                                    for entry in iterator
                                    if entry.name.casefold() not in ONEDRIVE_INTERNAL_NAMES
                                ),
                                key=lambda value: value.name.casefold(),
                            )
                    except OSError as exc:
                        add_issue(
                            IssueCode.ENUMERATION_ERROR,
                            IssueSeverity.BLOCKING,
                            f"cannot enumerate directory: {exc}",
                            rel_path,
                            details=_error_details(exc, directory),
                            operational=True,
                        )
                        continue

                    if len(entries) > MAX_WIKI_CHILDREN:
                        add_issue(
                            IssueCode.WIKI_CHILD_LIMIT,
                            IssueSeverity.BLOCKING,
                            f"directory has {len(entries)} children; Wiki allows "
                            f"at most {MAX_WIKI_CHILDREN}",
                            rel_path,
                            details={
                                "children": len(entries),
                                "limit": MAX_WIKI_CHILDREN,
                            },
                        )

                    child_directories: list[tuple[Path, str, int]] = []
                    for entry in entries:
                        check_cancel()
                        child_rel = f"{rel_path}/{entry.name}" if rel_path else entry.name
                        try:
                            if entry.is_symlink():
                                add_issue(
                                    IssueCode.SYMLINK_SKIPPED,
                                    IssueSeverity.WARNING,
                                    "symbolic links and junctions are not followed",
                                    child_rel,
                                )
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                child_directories.append((Path(entry.path), child_rel, depth + 1))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                add_issue(
                                    IssueCode.SYMLINK_SKIPPED,
                                    IssueSeverity.WARNING,
                                    "unsupported filesystem entry was skipped",
                                    child_rel,
                                )
                                continue
                            stat_result = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            add_issue(
                                IssueCode.STAT_ERROR,
                                IssueSeverity.BLOCKING,
                                f"cannot read file metadata: {exc}",
                                child_rel,
                                details=_error_details(exc, Path(entry.path)),
                                operational=True,
                            )
                            continue

                        file_attrs = int(getattr(stat_result, "st_file_attributes", 0) or 0)
                        file_offline, file_recall_open, file_recall_data = file_attribute_flags(
                            file_attrs
                        )
                        source_path = Path(entry.path)
                        size = int(stat_result.st_size)
                        file_depth = depth + 1
                        identity = (
                            None
                            if file_offline or file_recall_open or file_recall_data
                            else file_identity(stat_result, source_path)
                        )
                        cached_sha256 = self._cached_digest(
                            child_rel,
                            file_identity_value=identity,
                            size=size,
                            mtime_ns=int(stat_result.st_mtime_ns),
                            by_path=digest_by_path,
                            by_identity=digest_by_identity,
                        )
                        if cached_sha256 is not None:
                            try:
                                with source_path.open("rb") as stream:
                                    stream.read(1)
                            except OSError:
                                cached_sha256 = None
                        queue_file(
                            _FileCandidate(
                                path=source_path,
                                rel_path=child_rel,
                                parent_rel_path=rel_path,
                                name=entry.name,
                                depth=file_depth,
                                stat_result=stat_result,
                                file_attributes=file_attrs,
                                is_offline=file_offline,
                                is_recall_on_open=file_recall_open,
                                is_recall_on_data_access=file_recall_data,
                                file_identity=identity,
                                cached_sha256=cached_sha256,
                            )
                        )

                    # Reverse insertion keeps case-insensitive ascending traversal
                    # while still using a bounded depth-first stack.
                    stack.extend(reversed(child_directories))
                    if len(item_batch) >= self.batch_size:
                        flush()
                    emit_progress(rel_path)
                drain_hashes(force=True)
        except ScanCancelled:
            cancelled = True
            add_issue(
                IssueCode.SCAN_CANCELLED,
                IssueSeverity.BLOCKING,
                "scan was cancelled before the complete tree was inventoried",
                "",
                operational=True,
            )
        except Exception as exc:
            unexpected_error = exc
            add_issue(
                IssueCode.ENUMERATION_ERROR,
                IssueSeverity.BLOCKING,
                f"unexpected scan failure: {type(exc).__name__}: {exc}",
                "",
                operational=True,
            )
        finally:
            flush()
            hash_executor.shutdown(wait=True, cancel_futures=True)

        complete = not cancelled and unexpected_error is None and counts["operational_errors"] == 0
        project_status = ProjectStatus.SCANNED if complete else ProjectStatus.BLOCKED
        summary = {
            **counts,
            **hash_stats,
            "complete": complete,
            "cancelled": cancelled,
            "source_root": str(root),
        }
        emit_progress("", force=True)
        self.store.update_project(
            project_id,
            status=project_status,
            scan_complete=complete,
            current_scan_id=scan_id,
        )
        self.store.update_job_run(
            run.id,
            status=(
                RunStatus.CANCELLED
                if cancelled
                else RunStatus.COMPLETE
                if complete
                else RunStatus.FAILED
            ),
            total_items=counts["folders"] + counts["files"],
            completed_items=counts["folders"] + counts["files"],
            failed_items=counts["operational_errors"],
            summary=summary,
            error=(
                ""
                if unexpected_error is None
                else f"{type(unexpected_error).__name__}: {unexpected_error}"
            ),
            current_stage="COMPLETED" if complete else "FAILED",
            current_item="",
            last_message="盘点完成" if complete else "盘点未完成，请查看错误详情",
            heartbeat_at=utc_now(),
            finished_at=utc_now(),
        )
        self.store.append_audit(
            project_id,
            "scan.completed" if complete else "scan.incomplete",
            (
                "local inventory completed"
                if complete
                else "local inventory is incomplete and cannot be planned"
            ),
            level=AuditLevel.INFO if complete else AuditLevel.ERROR,
            job_run_id=run.id,
            payload=summary,
        )
        if unexpected_error is not None:
            raise unexpected_error
        return ScanResult(
            project_id=project_id,
            scan_id=scan_id,
            run_id=run.id,
            source_root=str(root),
            complete=complete,
            cancelled=cancelled,
            **counts,
        )

    @staticmethod
    def _add_preflight_issues(
        add_issue: Callable[..., None],
        *,
        name: str,
        rel_path: str,
        depth: int,
        is_file: bool,
        size: int | None,
        offline: bool,
        recall_open: bool,
        recall_data: bool,
    ) -> None:
        if len(name) > MAX_FEISHU_NAME_LENGTH:
            add_issue(
                IssueCode.NAME_TOO_LONG,
                IssueSeverity.BLOCKING,
                f"name has {len(name)} characters; maximum is {MAX_FEISHU_NAME_LENGTH}",
                rel_path,
                details={"length": len(name), "limit": MAX_FEISHU_NAME_LENGTH},
            )
        if depth > MAX_WIKI_LOCAL_DEPTH:
            add_issue(
                IssueCode.WIKI_DEPTH_LIMIT,
                IssueSeverity.BLOCKING,
                f"local depth {depth} cannot fit safely below the Wiki target",
                rel_path,
                details={"depth": depth, "safe_local_limit": MAX_WIKI_LOCAL_DEPTH},
            )
        if offline or recall_open or recall_data:
            add_issue(
                IssueCode.OFFLINE_PLACEHOLDER,
                IssueSeverity.BLOCKING,
                "OneDrive placeholder was not opened or hydrated",
                rel_path,
                details={
                    "offline": offline,
                    "recall_on_open": recall_open,
                    "recall_on_data_access": recall_data,
                },
            )
        if is_file and size == 0:
            add_issue(
                IssueCode.ZERO_BYTE_FILE,
                IssueSeverity.WARNING,
                (
                    "Feishu Drive rejects empty uploads; this file will be reported "
                    "and skipped without blocking the migration"
                ),
                rel_path,
                details={"migration_policy": "report_and_skip", "source_size": 0},
            )
