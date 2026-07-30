"""Read-only, resumable-friendly local inventory scanner."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable
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
        batch_size: int = 500,
        progress_interval: int = 100,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.store = store
        self.batch_size = batch_size
        self.progress_interval = max(1, progress_interval)

    def scan(
        self,
        project_id: str,
        source_root: str | Path | None = None,
        *,
        cancel: Callable[[], bool] | Any | None = None,
        progress: Callable[[ScanProgress], None] | None = None,
    ) -> ScanResult:
        project = self.store.get_project(project_id)
        root = Path(source_root or project.source_root).expanduser().absolute()
        scan_id = uuid.uuid4().hex
        run = self.store.create_job_run(
            project_id,
            RunType.SCAN,
            status=RunStatus.RUNNING,
            scan_id=scan_id,
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

        def check_cancel() -> None:
            if _is_cancelled(cancel):
                raise ScanCancelled("scan cancelled")

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
                                list(iterator), key=lambda value: value.name.casefold()
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
                        digest: str | None = None
                        if not (file_offline or file_recall_open or file_recall_data):
                            try:
                                digest = sha256_file(
                                    source_path,
                                    cancel_check=lambda: _is_cancelled(cancel),
                                )
                            except ScanCancelled:
                                raise
                            except OSError as exc:
                                add_issue(
                                    IssueCode.HASH_ERROR,
                                    IssueSeverity.BLOCKING,
                                    f"cannot hash file: {exc}",
                                    child_rel,
                                    details=_error_details(exc, source_path),
                                    operational=True,
                                )

                        size = int(stat_result.st_size)
                        file_depth = depth + 1
                        manual = (
                            size == 0
                            or file_offline
                            or file_recall_open
                            or file_recall_data
                            or digest is None
                            or len(entry.name) > MAX_FEISHU_NAME_LENGTH
                            or file_depth > MAX_WIKI_LOCAL_DEPTH
                        )
                        item_batch.append(
                            {
                                "kind": ItemKind.FILE,
                                "rel_path": child_rel,
                                "parent_rel_path": rel_path,
                                "name": entry.name,
                                "depth": file_depth,
                                "file_identity": (
                                    None
                                    if file_offline or file_recall_open or file_recall_data
                                    else file_identity(stat_result, source_path)
                                ),
                                "size": size,
                                "mtime_ns": int(stat_result.st_mtime_ns),
                                "sha256": digest,
                                "file_attributes": file_attrs,
                                "is_offline": file_offline,
                                "is_recall_on_open": file_recall_open,
                                "is_recall_on_data_access": file_recall_data,
                                "state": (
                                    InventoryState.MANUAL_ACTION
                                    if manual
                                    else InventoryState.DISCOVERED
                                ),
                            }
                        )
                        counts["files"] += 1
                        counts["bytes"] += size
                        self._add_preflight_issues(
                            add_issue,
                            name=entry.name,
                            rel_path=child_rel,
                            depth=file_depth,
                            is_file=True,
                            size=size,
                            offline=file_offline,
                            recall_open=file_recall_open,
                            recall_data=file_recall_data,
                        )
                        if len(item_batch) >= self.batch_size:
                            flush()
                        emit_progress(child_rel)

                    # Reverse insertion keeps case-insensitive ascending traversal
                    # while still using a bounded depth-first stack.
                    stack.extend(reversed(child_directories))
                    if len(item_batch) >= self.batch_size:
                        flush()
                    emit_progress(rel_path)
        except ScanCancelled:
            cancelled = True
            add_issue(
                IssueCode.SCAN_CANCELLED,
                IssueSeverity.BLOCKING,
                "scan was cancelled before the complete tree was inventoried",
                "",
                operational=True,
            )
        finally:
            flush()

        complete = not cancelled and counts["operational_errors"] == 0
        project_status = ProjectStatus.SCANNED if complete else ProjectStatus.BLOCKED
        summary = {
            **counts,
            "complete": complete,
            "cancelled": cancelled,
            "source_root": str(root),
        }
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
        emit_progress("", force=True)
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
                IssueSeverity.BLOCKING,
                "zero-byte files require an explicit manual decision",
                rel_path,
            )
