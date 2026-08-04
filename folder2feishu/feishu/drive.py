from __future__ import annotations

import hashlib
import math
import mimetypes
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .client import FeishuAPIClient
from .errors import (
    FeishuAmbiguousWriteError,
    FeishuError,
    FeishuProtocolError,
    ReconciliationError,
    UploadSessionError,
)
from .models import (
    NullPersistenceHooks,
    PersistenceHooks,
    RetryMode,
    StagingLocation,
    UploadSession,
)
from .rate_limit import RateLimitSet

SINGLE_UPLOAD_LIMIT = 20 * 1024 * 1024
CHUNK_SIZE = 4 * 1024 * 1024
UPLOAD_SESSION_LIFETIME_SECONDS = 24 * 60 * 60
UPLOAD_SESSION_EXPIRY_MARGIN_SECONDS = 10 * 60
MAX_FILE_NAME_CHARACTERS = 250
DRIVE_FOLDER_URL_SEGMENT = "folder"
STAGING_ROOT_NAME = "Folder2Feishu-Staging"
PERMISSION_RESOURCE_TYPES = frozenset(
    {
        "doc",
        "sheet",
        "file",
        "wiki",
        "bitable",
        "docx",
        "mindnote",
        "minutes",
        "slides",
    }
)
PERMISSION_ACTIONS = frozenset(
    {
        "view",
        "edit",
        "share",
        "comment",
        "export",
        "copy",
        "print",
        "manage_public",
    }
)


@dataclass(frozen=True, slots=True)
class StagedFile:
    file_token: str
    internal_name: str
    final_name: str


def parse_drive_folder_token(value: str) -> str:
    """Accept a Feishu Drive folder URL or a bare folder token."""

    raw = value.strip()
    if not raw:
        raise ValueError("飞书云盘文件夹地址不能为空")
    if "://" not in raw:
        if any(character.isspace() for character in raw) or "/" in raw:
            raise ValueError("飞书云盘文件夹 token 格式无效")
        return raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").casefold()
    if not (host.endswith(".feishu.cn") or host.endswith(".larksuite.com")):
        raise ValueError("只支持飞书或 Lark 云盘文件夹地址")
    parts = [part for part in parsed.path.split("/") if part]
    try:
        index = parts.index(DRIVE_FOLDER_URL_SEGMENT)
        token = parts[index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("地址中缺少 /drive/folder/<token>") from exc
    if not token or any(character.isspace() for character in token):
        raise ValueError("飞书云盘文件夹 token 格式无效")
    return token


def _validate_folder_token(token: str, field: str = "parent_node") -> str:
    token = token.strip()
    if not token:
        raise ValueError(f"{field} must be a non-empty Drive folder token")
    return token


def _validate_file_name(name: str) -> str:
    if not name or name in {".", ".."}:
        raise ValueError("file name must not be empty")
    if "\x00" in name or "/" in name or "\\" in name:
        raise ValueError("file name must not contain path separators")
    if len(name) > MAX_FILE_NAME_CHARACTERS:
        raise ValueError(f"file name exceeds {MAX_FILE_NAME_CHARACTERS} characters")
    return name


def _upload_attempt_callback(
    hooks: PersistenceHooks,
) -> Callable[[], None] | None:
    callback = getattr(hooks, "on_upload_attempt", None)
    return callback if callable(callback) else None


def deterministic_staging_name(project_id: str, item_key: str, original_name: str) -> str:
    """Return a stable collision-resistant staging name, capped at 250 chars."""

    original_name = _validate_file_name(original_name)
    digest = hashlib.sha256(
        f"{project_id}\0{item_key}".encode("utf-8", errors="strict")
    ).hexdigest()[:20]
    prefix = f".f2fw-{digest}--"
    available = MAX_FILE_NAME_CHARACTERS - len(prefix)
    if len(original_name) <= available:
        return prefix + original_name
    suffix = Path(original_name).suffix
    if len(suffix) >= available:
        suffix = suffix[: min(20, available // 3)]
    stem_length = available - len(suffix)
    return prefix + original_name[:stem_length] + suffix


def staging_project_folder_name(project_id: str) -> str:
    if not project_id:
        raise ValueError("project_id is required")
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:20]
    return f"project-{digest}"


def staging_shard_folder_name(shard_index: int) -> str:
    if shard_index < 0:
        raise ValueError("shard_index must not be negative")
    return f"shard-{shard_index:06d}"


class DriveService:
    def __init__(
        self,
        api: FeishuAPIClient,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.api = api
        self._now = now
        # Staging folders are stable for the lifetime of a migration project.
        # Resolving the same root/project/shard hierarchy for every file used to
        # add four or more Drive reads before each upload.  Keep a process-local
        # cache and serialize the rare cache miss so concurrent upload workers
        # cannot create duplicate staging folders.
        self._staging_lock = threading.RLock()
        self._root_folder_token = ""
        self._staging_root_token = ""
        self._staging_project_tokens: dict[str, str] = {}
        self._staging_shard_tokens: dict[tuple[str, int], str] = {}

    def get_root_folder_token(self) -> str:
        body = self.api.request(
            "GET",
            "/drive/explorer/v2/root_folder/meta",
            retry_mode=RetryMode.SAFE,
        )
        data = body.get("data") or {}
        token = str(data.get("token") or data.get("folder_token") or "")
        if not token:
            raise FeishuProtocolError("Drive root-folder response is missing a folder token")
        return token

    def get_current_user_info(self) -> dict[str, Any]:
        """Return the OAuth user's profile without switching identities."""

        body = self.api.request(
            "GET",
            "/authen/v1/user_info",
            retry_mode=RetryMode.SAFE,
        )
        data = body.get("data")
        if not isinstance(data, dict):
            raise FeishuProtocolError("current-user response is missing user information")
        return data

    def get_quota_detail(self, quota_detail_id: str) -> dict[str, Any]:
        """Return Drive quota details for the fixed OAuth user.

        Feishu requires ``quota_detail_id`` to be that current user's ``user_id``.
        The caller must not supply another user's ID.
        """

        quota_detail_id = quota_detail_id.strip()
        if not quota_detail_id:
            raise ValueError("quota_detail_id (the current user's user_id) is required")
        body = self.api.request(
            "GET",
            f"/drive/v2/quota_details/{quota_detail_id}",
            retry_mode=RetryMode.SAFE,
        )
        data = body.get("data")
        if not isinstance(data, dict):
            raise FeishuProtocolError("Drive quota response is missing quota details")
        return data

    def get_current_user_quota(self) -> dict[str, Any]:
        """Resolve the current user ID and return that user's Drive quota.

        The ``contact:user.employee_id:readonly`` scope is needed for
        ``user_id`` to appear in the user-info response. Keeping this failure
        explicit prevents silently treating an unknown quota as sufficient
        capacity.
        """

        user_info = self.get_current_user_info()
        user_id = str(user_info.get("user_id") or "")
        if not user_id:
            raise FeishuProtocolError(
                "Feishu did not return the current user_id; enable the "
                "contact:user.employee_id:readonly scope and re-authorize "
                "before running capacity preflight"
            )
        return self.get_quota_detail(user_id)

    def has_permission(
        self,
        token: str,
        *,
        resource_type: str,
        action: str,
    ) -> bool:
        """Check the fixed OAuth user's permission on a Drive or Wiki object."""

        token = token.strip()
        if not token:
            raise ValueError("document token is required")
        if resource_type not in PERMISSION_RESOURCE_TYPES:
            raise ValueError(f"unsupported permission resource type: {resource_type}")
        if action not in PERMISSION_ACTIONS:
            raise ValueError(f"unsupported permission action: {action}")
        body = self.api.request(
            "GET",
            f"/drive/v1/permissions/{token}/members/auth",
            params={"type": resource_type, "action": action},
            retry_mode=RetryMode.SAFE,
        )
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("auth_result"), bool):
            raise FeishuProtocolError("permission response is missing auth_result")
        return data["auth_result"]

    def can_edit_wiki(self, wiki_node_token: str) -> bool:
        return self.has_permission(
            wiki_node_token,
            resource_type="wiki",
            action="edit",
        )

    def list_folder(self, folder_token: str) -> list[dict[str, Any]]:
        folder_token = _validate_folder_token(folder_token, "folder_token")
        files: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "folder_token": folder_token,
                "page_size": 200,
            }
            if page_token:
                params["page_token"] = page_token
            body = self.api.request(
                "GET",
                "/drive/v1/files",
                params=params,
                retry_mode=RetryMode.SAFE,
            )
            data = body.get("data") or {}
            page = data.get("files") or data.get("items") or []
            if not isinstance(page, list):
                raise FeishuProtocolError("Drive file list is malformed")
            files.extend(item for item in page if isinstance(item, dict))
            if not data.get("has_more"):
                break
            page_token = str(data.get("next_page_token") or data.get("page_token") or "")
            if not page_token:
                raise FeishuProtocolError("Drive reported has_more without a next page token")
        return files

    def find_by_name(
        self,
        folder_token: str,
        name: str,
        *,
        object_type: str | None = None,
    ) -> dict[str, Any] | None:
        matches = [
            item
            for item in self.list_folder(folder_token)
            if item.get("name") == name and (object_type is None or item.get("type") == object_type)
        ]
        if len(matches) > 1:
            raise ReconciliationError(
                f"multiple staging objects have the deterministic name {name!r}"
            )
        return matches[0] if matches else None

    def create_folder(self, parent_token: str, name: str) -> str:
        parent_token = _validate_folder_token(parent_token, "folder_token")
        if not name or len(name.encode("utf-8")) > 256:
            raise ValueError("Drive folder name must contain 1-256 UTF-8 bytes")
        body = self.api.request(
            "POST",
            "/drive/v1/files/create_folder",
            rate_group=RateLimitSet.DRIVE_WRITE,
            json={"name": name, "folder_token": parent_token},
            retry_mode=RetryMode.RATE_LIMIT,
        )
        data = body.get("data") or {}
        token = str(data.get("token") or data.get("folder_token") or "")
        if not token:
            raise FeishuProtocolError("create-folder response is missing the new folder token")
        return token

    def ensure_folder(self, parent_token: str, name: str) -> str:
        existing = self.find_by_name(parent_token, name, object_type="folder")
        if existing:
            token = str(existing.get("token") or "")
            if not token:
                raise FeishuProtocolError("existing Drive folder has no token")
            return token
        try:
            return self.create_folder(parent_token, name)
        except FeishuAmbiguousWriteError:
            # A transport failure can occur after Feishu commits the folder.
            existing = self.find_by_name(parent_token, name, object_type="folder")
            if existing and existing.get("token"):
                return str(existing["token"])
            raise

    def ensure_staging(self, project_id: str, *, shard_index: int = 0) -> StagingLocation:
        if not project_id:
            raise ValueError("project_id is required")
        if shard_index < 0:
            raise ValueError("shard_index must not be negative")
        cache_key = (project_id, shard_index)
        with self._staging_lock:
            cached_shard = self._staging_shard_tokens.get(cache_key)
            cached_project = self._staging_project_tokens.get(project_id)
            if cached_shard and cached_project and self._staging_root_token:
                return StagingLocation(
                    root_token=self._staging_root_token,
                    project_token=cached_project,
                    shard_token=cached_shard,
                )

            if not self._root_folder_token:
                self._root_folder_token = self.get_root_folder_token()
            if not self._staging_root_token:
                self._staging_root_token = self.ensure_folder(
                    self._root_folder_token,
                    STAGING_ROOT_NAME,
                )
            project_folder = cached_project
            if not project_folder:
                project_folder = self.ensure_folder(
                    self._staging_root_token,
                    staging_project_folder_name(project_id),
                )
                self._staging_project_tokens[project_id] = project_folder
            shard_folder = self.ensure_folder(
                project_folder,
                staging_shard_folder_name(shard_index),
            )
            self._staging_shard_tokens[cache_key] = shard_folder
            return StagingLocation(
                root_token=self._staging_root_token,
                project_token=project_folder,
                shard_token=shard_folder,
            )

    def clear_staging_cache(self) -> None:
        """Forget cached folder tokens after an operator repairs Drive state."""

        with self._staging_lock:
            self._root_folder_token = ""
            self._staging_root_token = ""
            self._staging_project_tokens.clear()
            self._staging_shard_tokens.clear()

    def rename_file(self, file_token: str, new_title: str, *, object_type: str = "file") -> str:
        if not file_token.strip():
            raise ValueError("file_token is required")
        _validate_file_name(new_title)
        self.api.request(
            "PATCH",
            f"/drive/v1/files/{file_token}",
            rate_group=RateLimitSet.DRIVE_WRITE,
            params={"type": object_type},
            json={"new_title": new_title},
            retry_mode=RetryMode.RATE_LIMIT,
        )
        return file_token

    def move_file(
        self,
        file_token: str,
        *,
        object_type: str,
        destination_folder_token: str,
    ) -> str:
        """Move a file or folder and return an optional asynchronous task ID."""

        if not file_token.strip():
            raise ValueError("file_token is required")
        if object_type not in {"file", "folder"}:
            raise ValueError("object_type must be file or folder")
        body = self.api.request(
            "POST",
            f"/drive/v1/files/{file_token}/move",
            rate_group=RateLimitSet.DRIVE_WRITE,
            json={
                "type": object_type,
                "folder_token": _validate_folder_token(destination_folder_token),
            },
            retry_mode=RetryMode.RATE_LIMIT,
        )
        return str((body.get("data") or {}).get("task_id") or "")

    def wait_task(self, task_id: str, *, timeout_seconds: float = 120.0) -> None:
        if not task_id:
            return
        deadline = self._now() + timeout_seconds
        while self._now() < deadline:
            body = self.api.request(
                "GET",
                "/drive/v1/files/task_check",
                params={"task_id": task_id},
                retry_mode=RetryMode.SAFE,
            )
            status = str((body.get("data") or {}).get("status") or "").casefold()
            if status in {"success", "succeeded", "done"}:
                return
            if status in {"failed", "failure", "error"}:
                raise FeishuError(f"飞书云盘异步任务失败：{task_id}")
            time.sleep(1.0)
        raise FeishuError(f"飞书云盘异步任务等待超时：{task_id}")

    def reconcile_child(
        self,
        parent_token: str,
        *,
        object_token: str,
        name: str,
        object_type: str,
    ) -> dict[str, Any]:
        """Verify the mapped child still has the expected parent, name and type."""

        for item in self.list_folder(parent_token):
            if str(item.get("token") or "") != object_token:
                continue
            differences: list[str] = []
            if str(item.get("name") or "") != name:
                differences.append("name")
            if str(item.get("type") or "") != object_type:
                differences.append("type")
            return {"matched": not differences, "differences": differences, "item": item}
        return {"matched": False, "differences": ["missing"], "item": None}

    def reconcile_staging_upload(self, parent_node: str, internal_name: str) -> str | None:
        item = self.find_by_name(
            _validate_folder_token(parent_node),
            internal_name,
            object_type="file",
        )
        if not item:
            return None
        token = str(item.get("token") or "")
        if not token:
            raise FeishuProtocolError("staging file has no token")
        return token

    def stage_file(
        self,
        local_path: str | Path,
        *,
        parent_node: str,
        project_id: str,
        item_key: str,
        original_name: str | None = None,
        resume_session: UploadSession | None = None,
        hooks: PersistenceHooks | None = None,
    ) -> StagedFile:
        path = Path(local_path)
        final_name = _validate_file_name(original_name or path.name)
        internal_name = deterministic_staging_name(project_id, item_key, final_name)
        active_hooks = hooks or NullPersistenceHooks()

        existing_token = self.reconcile_staging_upload(parent_node, internal_name)
        if existing_token:
            active_hooks.on_file_token(existing_token)
            self.rename_file(existing_token, final_name)
            return StagedFile(existing_token, internal_name, final_name)

        try:
            token = self.upload_file(
                path,
                file_name=internal_name,
                parent_node=parent_node,
                resume_session=resume_session,
                hooks=active_hooks,
            )
        except FeishuAmbiguousWriteError:
            reconciled_token = self.reconcile_staging_upload(parent_node, internal_name)
            if not reconciled_token:
                raise
            token = reconciled_token
            active_hooks.on_file_token(token)

        self.rename_file(token, final_name)
        return StagedFile(token, internal_name, final_name)

    def upload_file(
        self,
        local_path: str | Path,
        *,
        file_name: str,
        parent_node: str,
        resume_session: UploadSession | None = None,
        hooks: PersistenceHooks | None = None,
    ) -> str:
        path = Path(local_path)
        parent_node = _validate_folder_token(parent_node)
        file_name = _validate_file_name(file_name)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise FeishuError(f"unable to read local file metadata: {path.name}") from exc
        if size <= 0:
            raise ValueError("Feishu does not support zero-byte files")
        active_hooks = hooks or NullPersistenceHooks()
        if size <= SINGLE_UPLOAD_LIMIT:
            return self._upload_all(path, file_name, size, parent_node, active_hooks)
        return self._upload_multipart(
            path,
            file_name,
            size,
            parent_node,
            resume_session,
            active_hooks,
        )

    def _upload_all(
        self,
        path: Path,
        file_name: str,
        size: int,
        parent_node: str,
        hooks: PersistenceHooks,
    ) -> str:
        parent_node = _validate_folder_token(parent_node)
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        try:
            with path.open("rb") as stream:
                body = self.api.request(
                    "POST",
                    "/drive/v1/files/upload_all",
                    rate_group=RateLimitSet.DRIVE_UPLOAD,
                    retry_mode=RetryMode.RATE_LIMIT,
                    before_attempt=_upload_attempt_callback(hooks),
                    data={
                        "file_name": file_name,
                        "parent_type": "explorer",
                        "parent_node": parent_node,
                        "size": str(size),
                    },
                    files={"file": (file_name, stream, mime_type)},
                )
        except OSError as exc:
            raise FeishuError(f"unable to read local file: {path.name}") from exc
        token = str((body.get("data") or {}).get("file_token") or "")
        if not token:
            raise FeishuProtocolError("upload response is missing file_token")
        hooks.on_file_token(token)
        return token

    def _upload_multipart(
        self,
        path: Path,
        file_name: str,
        size: int,
        parent_node: str,
        resume_session: UploadSession | None,
        hooks: PersistenceHooks,
    ) -> str:
        session = self._usable_session(
            resume_session, file_name=file_name, size=size, parent_node=parent_node
        )
        if session is None:
            body = self.api.request(
                "POST",
                "/drive/v1/files/upload_prepare",
                rate_group=RateLimitSet.DRIVE_UPLOAD,
                retry_mode=RetryMode.RATE_LIMIT,
                before_attempt=_upload_attempt_callback(hooks),
                json={
                    "file_name": file_name,
                    "parent_type": "explorer",
                    "parent_node": _validate_folder_token(parent_node),
                    "size": size,
                },
            )
            data = body.get("data") or {}
            upload_id = str(data.get("upload_id") or "")
            block_size = int(data.get("block_size") or 0)
            block_num = int(data.get("block_num") or 0)
            if not upload_id or not block_size or not block_num:
                raise FeishuProtocolError("multipart prepare response is missing upload strategy")
            if block_size != CHUNK_SIZE:
                raise FeishuProtocolError(
                    f"Feishu returned unexpected multipart block size {block_size}"
                )
            expected_blocks = math.ceil(size / CHUNK_SIZE)
            if block_num != expected_blocks:
                raise FeishuProtocolError(
                    "Feishu multipart block count does not match the local file"
                )
            session = UploadSession(
                upload_id=upload_id,
                parent_node=parent_node,
                file_name=file_name,
                size=size,
                block_size=CHUNK_SIZE,
                block_num=block_num,
                completed_parts=frozenset(),
                expires_at=(
                    self._now()
                    + UPLOAD_SESSION_LIFETIME_SECONDS
                    - UPLOAD_SESSION_EXPIRY_MARGIN_SECONDS
                ),
            )
            hooks.on_upload_session(session)

        try:
            with path.open("rb") as stream:
                for sequence in range(session.block_num):
                    if sequence in session.completed_parts:
                        continue
                    offset = sequence * session.block_size
                    expected_size = min(session.block_size, size - offset)
                    stream.seek(offset)
                    chunk = stream.read(expected_size)
                    if len(chunk) != expected_size:
                        raise UploadSessionError(
                            "local file changed or became unreadable during upload"
                        )
                    self.api.request(
                        "POST",
                        "/drive/v1/files/upload_part",
                        rate_group=RateLimitSet.DRIVE_UPLOAD,
                        retry_mode=RetryMode.ALWAYS,
                        before_attempt=_upload_attempt_callback(hooks),
                        data={
                            "upload_id": session.upload_id,
                            "seq": str(sequence),
                            "size": str(len(chunk)),
                        },
                        files={
                            "file": (
                                f"part-{sequence}",
                                chunk,
                                "application/octet-stream",
                            )
                        },
                    )
                    hooks.on_upload_part(session.upload_id, sequence)
        except OSError as exc:
            raise FeishuError(f"unable to read local file: {path.name}") from exc

        body = self.api.request(
            "POST",
            "/drive/v1/files/upload_finish",
            rate_group=RateLimitSet.DRIVE_UPLOAD,
            retry_mode=RetryMode.RATE_LIMIT,
            before_attempt=_upload_attempt_callback(hooks),
            json={
                "upload_id": session.upload_id,
                "block_num": session.block_num,
            },
        )
        token = str((body.get("data") or {}).get("file_token") or "")
        if not token:
            raise FeishuProtocolError("multipart finish response is missing file_token")
        hooks.on_file_token(token)
        return token

    def _usable_session(
        self,
        session: UploadSession | None,
        *,
        file_name: str,
        size: int,
        parent_node: str,
    ) -> UploadSession | None:
        if session is None or self._now() >= session.expires_at:
            return None
        if (
            session.file_name != file_name
            or session.size != size
            or session.parent_node != parent_node
            or session.block_size != CHUNK_SIZE
            or session.block_num != math.ceil(size / CHUNK_SIZE)
        ):
            raise UploadSessionError("stored upload session does not match the current local file")
        if any(
            sequence < 0 or sequence >= session.block_num for sequence in session.completed_parts
        ):
            raise UploadSessionError("stored upload session contains invalid parts")
        return session
