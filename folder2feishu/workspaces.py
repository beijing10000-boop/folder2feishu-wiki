"""Switchable, folder-backed runtime workspaces.

Each direct child of the projects root is an isolated Folder2Feishu runtime:
its SQLite ledger, settings, encrypted credentials, quota state and exports all
remain under that child directory.  The HTTP application keeps one active
workspace at a time and refuses to switch while durable work is active.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

from .application import ApplicationServices
from .runtime import RuntimePaths

CONTROL_FILE = ".folder2feishu-active.json"
INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
ACTIVE_JOB_STATES = {"QUEUED", "RUNNING", "PAUSED"}


def default_projects_root() -> Path:
    configured = os.environ.get("FOLDER2FEISHU_PROJECTS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        return Path(r"D:\Folder2FeishuDrive\Projects").resolve()
    local_data = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return (local_data / "Folder2FeishuDrive" / "Projects").resolve()


def validate_workspace_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("项目名称不能为空")
    if name in {".", ".."} or INVALID_WINDOWS_CHARS.search(name):
        raise ValueError("项目名称包含 Windows 文件夹不支持的字符")
    if name.endswith((" ", ".")):
        raise ValueError("项目名称不能以空格或句点结尾")
    if name.split(".", 1)[0].upper() in RESERVED_WINDOWS_NAMES:
        raise ValueError("项目名称是 Windows 保留名称，请更换")
    if name.startswith("."):
        raise ValueError("项目名称不能以句点开头")
    if len(name) > 80:
        raise ValueError("项目名称不能超过 80 个字符")
    return name


class WorkspaceManager:
    """Own the active :class:`ApplicationServices` and isolated runtime folders."""

    def __init__(
        self,
        projects_root: str | Path,
        *,
        initial_runtime: str | Path | None = None,
    ) -> None:
        self.projects_root = Path(projects_root).expanduser().resolve()
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.service_paths = RuntimePaths.discover(self.projects_root / ".service").ensure()
        self._lock = threading.RLock()
        self._active_name = ""
        self._active: ApplicationServices | None = None
        self._retired: list[ApplicationServices] = []

        preferred = self._initial_name(initial_runtime) or self._saved_name()
        available = [item["folder_name"] for item in self.list_workspaces()["items"]]
        if preferred in available:
            self._activate(preferred, persist=False)
        elif available:
            self._activate(available[0], persist=True)

    @property
    def has_active(self) -> bool:
        return self._active is not None

    @property
    def active_name(self) -> str:
        return self._active_name

    @property
    def active_services(self) -> ApplicationServices:
        with self._lock:
            if self._active is None:
                raise ValueError("请先选择或新建一个数据项目")
            return self._active

    def __getattr__(self, name: str) -> Any:
        # Existing API handlers deliberately remain unaware of the workspace
        # layer and resolve every store/service access against the active data
        # folder at call time.
        return getattr(self.active_services, name)

    def list_workspaces(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for child in sorted(self.projects_root.iterdir(), key=lambda path: path.name.casefold()):
            if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
                continue
            items.append(self._workspace_payload(child))
        return {
            "projects_root": str(self.projects_root),
            "active_folder_name": self._active_name,
            "items": items,
        }

    def create_workspace(self, name: str) -> dict[str, Any]:
        clean_name = validate_workspace_name(name)
        target = self._child_path(clean_name)
        if target.exists():
            raise ValueError("同名数据项目已存在，请直接选择现有项目")
        target.mkdir(parents=False, exist_ok=False)
        try:
            self._activate(clean_name, persist=True)
        except Exception:
            # A newly-created empty folder is safe to remove if initialization
            # itself fails; no user data can exist in it yet.
            with suppress(OSError):
                target.rmdir()
            raise
        return self.current_payload()

    def select_workspace(self, name: str) -> dict[str, Any]:
        clean_name = validate_workspace_name(name)
        target = self._child_path(clean_name)
        if not target.is_dir() or target.is_symlink():
            raise KeyError(f"unknown workspace: {clean_name}")
        if clean_name != self._active_name:
            self._activate(clean_name, persist=True)
        return self.current_payload()

    def current_payload(self) -> dict[str, Any]:
        if not self._active_name:
            return {
                "projects_root": str(self.projects_root),
                "active_folder_name": "",
                "active": None,
            }
        return {
            "projects_root": str(self.projects_root),
            "active_folder_name": self._active_name,
            "active": self._workspace_payload(self._child_path(self._active_name)),
        }

    def recover_active_jobs(self) -> int:
        if not self.has_active:
            return 0
        return self.active_services.store.interrupt_orphaned_job_runs(stale_after_seconds=0)

    def close(self) -> None:
        with self._lock:
            services = [*self._retired]
            if self._active is not None:
                services.append(self._active)
            self._retired = []
            self._active = None
        for service in services:
            service.close()

    def _activate(self, name: str, *, persist: bool) -> None:
        target = self._child_path(name)
        target.mkdir(parents=False, exist_ok=True)
        with self._lock:
            if self._active_name == name and self._active is not None:
                return
            if self._active is not None:
                statuses = self._active.store.job_status_counts()
                blocking = sum(statuses.get(status, 0) for status in ACTIVE_JOB_STATES)
                if blocking:
                    raise ValueError(
                        "当前数据项目仍有运行、排队或暂停中的任务。"
                        "请先在运行对账页停止任务，再切换项目。"
                    )

            replacement = ApplicationServices(paths=RuntimePaths.discover(target))
            interrupted = replacement.store.interrupt_orphaned_job_runs(stale_after_seconds=0)
            previous = self._active
            self._active = replacement
            self._active_name = name
            if persist:
                self._save_name(name)
            if previous is not None:
                self._retired.append(previous)
                # Keep a short grace window for an already-started local status
                # request, while still bounding open SQLite/HTTP resources.
                while len(self._retired) > 2:
                    self._retired.pop(0).close()
            if interrupted:
                # The interrupted state is already durable; the UI will expose
                # the normal resume control for this workspace.
                pass

    def _workspace_payload(self, path: Path) -> dict[str, Any]:
        project_name = ""
        project_count = 0
        if path.name == self._active_name and self._active is not None:
            projects = self._active.store.list_projects()
            project_count = len(projects)
            project_name = projects[0].name if projects else ""
        elif (path / "ledger.sqlite3").is_file():
            # The selector must be able to show a useful project name without
            # opening/migrating every inactive SQLite ledger.  A read-only URI
            # keeps discovery side-effect free and leaves the active writer
            # connection isolated to the selected workspace.
            try:
                database_uri = (path / "ledger.sqlite3").resolve().as_uri() + "?mode=ro"
                with sqlite3.connect(database_uri, uri=True, timeout=1.0) as connection:
                    row = connection.execute(
                        "SELECT name FROM projects ORDER BY created_at LIMIT 1"
                    ).fetchone()
                    count_row = connection.execute("SELECT COUNT(*) FROM projects").fetchone()
                project_name = str(row[0]) if row else ""
                project_count = int(count_row[0]) if count_row else 0
            except (OSError, sqlite3.Error, TypeError, ValueError):
                # A corrupt or legacy ledger is still listed. Selecting it
                # will surface the normal database initialization error with
                # full context instead of hiding the data folder here.
                pass
        return {
            "folder_name": path.name,
            "folder_path": str(path),
            "project_name": project_name,
            "project_count": project_count,
            "has_ledger": (path / "ledger.sqlite3").is_file(),
            "has_settings": (path / "settings.json").is_file(),
            "active": path.name == self._active_name,
        }

    def _child_path(self, name: str) -> Path:
        child = (self.projects_root / name).resolve()
        if child.parent != self.projects_root:
            raise ValueError("项目数据目录必须位于统一 Projects 目录下")
        return child

    def _initial_name(self, initial_runtime: str | Path | None) -> str:
        if not initial_runtime:
            return ""
        runtime = Path(initial_runtime).expanduser().resolve()
        return runtime.name if runtime.parent == self.projects_root and runtime.is_dir() else ""

    def _saved_name(self) -> str:
        try:
            payload = json.loads((self.projects_root / CONTROL_FILE).read_text(encoding="utf-8"))
            return str(payload.get("active_folder_name") or "")
        except (OSError, ValueError, TypeError):
            return ""

    def _save_name(self, name: str) -> None:
        target = self.projects_root / CONTROL_FILE
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"active_folder_name": name}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
