from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlparse

from .client import FeishuAPIClient
from .errors import (
    FeishuAmbiguousWriteError,
    FeishuAPIError,
    FeishuError,
    FeishuProtocolError,
    WikiMoveTaskFailedError,
)
from .models import (
    MoveSubmission,
    NullPersistenceHooks,
    PersistenceHooks,
    ReconcileResult,
    ReconcileStatus,
    RetryMode,
    WikiReference,
)
from .rate_limit import RateLimitSet

_WIKI_TOKEN = re.compile(r"^[A-Za-z0-9_-]{10,999}$")
_ALLOWED_WIKI_HOSTS = ("feishu.cn", "larksuite.com")
_NON_RETRYABLE_MOVE_FAILURES = (
    "already in wiki",
    "permission denied",
    "not support advanced bitable",
    "source not exist",
    "not support obj type",
    "tree limit",
)


def parse_wiki_reference(value: str) -> WikiReference:
    raw = (value or "").strip()
    if _WIKI_TOKEN.fullmatch(raw):
        return WikiReference(node_token=raw)
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(
        host == suffix or host.endswith(f".{suffix}") for suffix in _ALLOWED_WIKI_HOSTS
    ):
        raise ValueError("Wiki URL must be an HTTPS feishu.cn or larksuite.com URL")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        wiki_index = parts.index("wiki")
        token = parts[wiki_index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("URL does not contain a Wiki node token") from exc
    if not _WIKI_TOKEN.fullmatch(token):
        raise ValueError("Wiki node token has an invalid format")
    return WikiReference(node_token=token, host=host)


def parse_wiki_token(value: str) -> str:
    return parse_wiki_reference(value).node_token


class WikiService:
    def __init__(
        self,
        api: FeishuAPIClient,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self._sleep = api.wait if sleeper is time.sleep else sleeper
        self._monotonic = monotonic

    def get_node(self, node_token: str, *, obj_type: str = "wiki") -> dict[str, Any]:
        if not node_token.strip():
            raise ValueError("node_token is required")
        body = self.api.request(
            "GET",
            "/wiki/v2/spaces/get_node",
            rate_group=RateLimitSet.WIKI_READ,
            retry_mode=RetryMode.SAFE,
            params={"token": node_token, "obj_type": obj_type},
        )
        node = (body.get("data") or {}).get("node")
        if not isinstance(node, dict) or not node.get("node_token"):
            raise FeishuProtocolError("Wiki get-node response is missing node")
        return node

    def list_children(self, space_id: str, parent_node_token: str) -> list[dict[str, Any]]:
        if not space_id.strip():
            raise ValueError("space_id is required")
        if not parent_node_token.strip():
            raise ValueError("parent_node_token is required")
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "parent_node_token": parent_node_token,
                "page_size": 50,
            }
            if page_token:
                params["page_token"] = page_token
            body = self.api.request(
                "GET",
                f"/wiki/v2/spaces/{space_id}/nodes",
                rate_group=RateLimitSet.WIKI_READ,
                retry_mode=RetryMode.SAFE,
                params=params,
            )
            data = body.get("data") or {}
            page = data.get("items") or []
            if not isinstance(page, list):
                raise FeishuProtocolError("Wiki child-node list is malformed")
            items.extend(item for item in page if isinstance(item, dict))
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise FeishuProtocolError("Wiki reported has_more without a page token")
        return items

    def find_child(
        self,
        space_id: str,
        parent_node_token: str,
        *,
        title: str,
        obj_type: str | None = None,
    ) -> dict[str, Any] | None:
        matches = [
            node
            for node in self.list_children(space_id, parent_node_token)
            if node.get("title") == title and (obj_type is None or node.get("obj_type") == obj_type)
        ]
        if len(matches) > 1:
            raise FeishuError(
                f"multiple Wiki children named {title!r}; manual reconciliation required"
            )
        return matches[0] if matches else None

    def create_docx_node(self, space_id: str, title: str, parent_node_token: str) -> dict[str, Any]:
        if not title:
            raise ValueError("Wiki node title is required")
        if not parent_node_token.strip():
            raise ValueError("parent_node_token is required")
        body = self.api.request(
            "POST",
            f"/wiki/v2/spaces/{space_id}/nodes",
            rate_group=RateLimitSet.WIKI_CREATE,
            retry_mode=RetryMode.NEVER,
            json={
                "obj_type": "docx",
                "node_type": "origin",
                "parent_node_token": parent_node_token,
                "title": title,
            },
        )
        node = (body.get("data") or {}).get("node")
        if not isinstance(node, dict) or not node.get("node_token"):
            raise FeishuProtocolError("create Wiki node response is missing node_token")
        return node

    def ensure_docx_node(self, space_id: str, title: str, parent_node_token: str) -> dict[str, Any]:
        existing = self.find_child(space_id, parent_node_token, title=title, obj_type="docx")
        if existing:
            return existing
        try:
            return self.create_docx_node(space_id, title, parent_node_token)
        except FeishuAmbiguousWriteError:
            # The POST may have committed before the connection failed.
            existing = self.find_child(space_id, parent_node_token, title=title, obj_type="docx")
            if existing:
                return existing
            raise

    def move_docs_to_wiki(
        self,
        space_id: str,
        *,
        obj_token: str,
        parent_wiki_token: str,
        obj_type: str = "file",
        apply: bool = False,
        hooks: PersistenceHooks | None = None,
    ) -> MoveSubmission:
        if not all(value.strip() for value in (space_id, obj_token, parent_wiki_token)):
            raise ValueError("space_id, obj_token and parent_wiki_token are required")
        active_hooks = hooks or NullPersistenceHooks()
        body = self.api.request(
            "POST",
            f"/wiki/v2/spaces/{space_id}/nodes/move_docs_to_wiki",
            rate_group=RateLimitSet.WIKI_WRITE,
            retry_mode=RetryMode.RATE_LIMIT,
            json={
                "parent_wiki_token": parent_wiki_token,
                "obj_type": obj_type,
                "obj_token": obj_token,
                "apply": apply,
            },
        )
        data = body.get("data") or {}
        wiki_token = str(data.get("wiki_token") or "")
        if wiki_token:
            active_hooks.on_wiki_token(wiki_token)
            return MoveSubmission(wiki_token=wiki_token)
        task_id = str(data.get("task_id") or "")
        if task_id:
            active_hooks.on_wiki_task(task_id)
            return MoveSubmission(task_id=task_id)
        if data.get("applied") is True:
            return MoveSubmission(applied=True)
        raise FeishuProtocolError(
            "move-to-Wiki response has no wiki_token, task_id, or applied status"
        )

    def poll_move_task(
        self,
        task_id: str,
        *,
        timeout_seconds: float = 180,
        interval_seconds: float = 2,
        hooks: PersistenceHooks | None = None,
    ) -> str:
        if not task_id.strip():
            raise ValueError("task_id is required")
        if timeout_seconds <= 0 or interval_seconds < 0:
            raise ValueError("invalid polling interval or timeout")
        active_hooks = hooks or NullPersistenceHooks()
        deadline = self._monotonic() + timeout_seconds
        while self._monotonic() < deadline:
            body = self.api.request(
                "GET",
                f"/wiki/v2/tasks/{task_id}",
                rate_group=RateLimitSet.WIKI_READ,
                retry_mode=RetryMode.SAFE,
                params={"task_type": "move"},
            )
            data = body.get("data") or {}
            token, failed_message = self._parse_move_task(data)
            if token:
                active_hooks.on_wiki_token(token)
                return token
            if failed_message:
                normalized = failed_message.strip().casefold()
                retryable = normalized in {"failure", "failed"}
                if any(marker in normalized for marker in _NON_RETRYABLE_MOVE_FAILURES):
                    retryable = False
                raise WikiMoveTaskFailedError(
                    f"move-to-Wiki task failed: {failed_message}",
                    retryable=retryable,
                )
            self._sleep(interval_seconds)
        raise FeishuError("move-to-Wiki task is still pending; keep the task_id and resume polling")

    def move_file_to_wiki(
        self,
        space_id: str,
        *,
        file_token: str,
        parent_wiki_token: str,
        hooks: PersistenceHooks | None = None,
        timeout_seconds: float = 180,
        existing_task_id: str | None = None,
    ) -> str:
        active_hooks = hooks or NullPersistenceHooks()
        if existing_task_id:
            # The ledger persisted task_id before any polling. A process restart
            # resumes here and must not issue move_docs_to_wiki a second time.
            try:
                return self.poll_move_task(
                    existing_task_id,
                    timeout_seconds=timeout_seconds,
                    hooks=active_hooks,
                )
            except WikiMoveTaskFailedError as exc:
                # A terminal task may still have committed the move before its
                # status became visible. Resolve by the immutable Drive object
                # token before deciding whether a replacement task is safe.
                reconciled = self.reconcile_object(
                    file_token,
                    obj_type="file",
                    expected_space_id=space_id,
                    expected_parent_token=parent_wiki_token,
                )
                if (
                    reconciled.status == ReconcileStatus.MATCH
                    and reconciled.node
                    and reconciled.node.get("node_token")
                ):
                    wiki_token = str(reconciled.node["node_token"])
                    active_hooks.on_wiki_token(wiki_token)
                    return wiki_token
                if reconciled.status == ReconcileStatus.CONFLICT:
                    raise WikiMoveTaskFailedError(
                        f"{exc}; remote object is in an unexpected Wiki location",
                        retryable=False,
                    ) from exc
                if not exc.retryable:
                    raise
                # Clearing is itself a durable checkpoint. Only an explicitly
                # retryable terminal failure that is absent remotely reaches a
                # single replacement submission below.
                active_hooks.on_wiki_task("")
        try:
            submission = self.move_docs_to_wiki(
                space_id,
                obj_token=file_token,
                obj_type="file",
                parent_wiki_token=parent_wiki_token,
                hooks=active_hooks,
            )
        except FeishuAmbiguousWriteError:
            # If the response was lost after the move committed, querying by
            # the Drive object token resolves the Wiki node without reposting.
            reconciled = self.reconcile_object(
                file_token,
                obj_type="file",
                expected_space_id=space_id,
                expected_parent_token=parent_wiki_token,
            )
            if (
                reconciled.status == ReconcileStatus.MATCH
                and reconciled.node
                and reconciled.node.get("node_token")
            ):
                wiki_token = str(reconciled.node["node_token"])
                active_hooks.on_wiki_token(wiki_token)
                return wiki_token
            raise
        if submission.wiki_token:
            return submission.wiki_token
        if submission.task_id:
            return self.poll_move_task(
                submission.task_id,
                timeout_seconds=timeout_seconds,
                hooks=active_hooks,
            )
        raise FeishuError("move permission request was submitted but the file is not in Wiki yet")

    def move_node(
        self,
        space_id: str,
        node_token: str,
        *,
        target_parent_token: str,
        target_space_id: str | None = None,
    ) -> str:
        if not all(value.strip() for value in (space_id, node_token, target_parent_token)):
            raise ValueError("space_id, node_token, and target_parent_token are required")
        body = self.api.request(
            "POST",
            f"/wiki/v2/spaces/{space_id}/nodes/{node_token}/move",
            rate_group=RateLimitSet.WIKI_WRITE,
            retry_mode=RetryMode.NEVER,
            json={
                "target_parent_token": target_parent_token,
                "target_space_id": target_space_id or space_id,
            },
        )
        node = (body.get("data") or {}).get("node") or {}
        returned = str(node.get("node_token") or node_token)
        if not returned:
            raise FeishuProtocolError("move Wiki node response has no node_token")
        return returned

    def archive_file_node(
        self,
        space_id: str,
        node_token: str,
        *,
        history_parent_token: str,
    ) -> str:
        """Move the old raw-file node under a mirrored history parent."""

        return self.move_node(
            space_id,
            node_token,
            target_parent_token=history_parent_token,
        )

    def rename_node(self, space_id: str, node_token: str, title: str) -> str:
        if not title:
            raise ValueError("Wiki node title is required")
        self.api.request(
            "POST",
            f"/wiki/v2/spaces/{space_id}/nodes/{node_token}/update_title",
            rate_group=RateLimitSet.WIKI_WRITE,
            retry_mode=RetryMode.NEVER,
            json={"title": title},
        )
        return node_token

    def reconcile_node(
        self,
        node_token: str,
        *,
        expected_space_id: str | None = None,
        expected_parent_token: str | None = None,
        expected_title: str | None = None,
        expected_obj_token: str | None = None,
    ) -> ReconcileResult:
        try:
            node = self.get_node(node_token)
        except FeishuAPIError as exc:
            if exc.code == 131005 or exc.status_code == 404:
                return ReconcileResult(status=ReconcileStatus.MISSING)
            raise
        differences: list[str] = []
        checks = (
            ("space_id", expected_space_id),
            ("parent_node_token", expected_parent_token),
            ("title", expected_title),
            ("obj_token", expected_obj_token),
        )
        for field, expected in checks:
            if expected is not None and str(node.get(field) or "") != expected:
                differences.append(field)
        return ReconcileResult(
            status=(ReconcileStatus.CONFLICT if differences else ReconcileStatus.MATCH),
            node=node,
            differences=tuple(differences),
        )

    def reconcile_object(
        self,
        obj_token: str,
        *,
        obj_type: str,
        expected_space_id: str | None = None,
        expected_parent_token: str | None = None,
        expected_title: str | None = None,
    ) -> ReconcileResult:
        """Resolve a Wiki node from its underlying Drive object token."""

        try:
            node = self.get_node(obj_token, obj_type=obj_type)
        except FeishuAPIError as exc:
            if exc.code == 131005 or exc.status_code == 404:
                return ReconcileResult(status=ReconcileStatus.MISSING)
            raise
        differences: list[str] = []
        checks = (
            ("space_id", expected_space_id),
            ("parent_node_token", expected_parent_token),
            ("title", expected_title),
        )
        for field, expected in checks:
            if expected is not None and str(node.get(field) or "") != expected:
                differences.append(field)
        if str(node.get("obj_token") or "") not in {"", obj_token}:
            differences.append("obj_token")
        return ReconcileResult(
            status=(ReconcileStatus.CONFLICT if differences else ReconcileStatus.MATCH),
            node=node,
            differences=tuple(differences),
        )

    @staticmethod
    def _parse_move_task(data: dict[str, Any]) -> tuple[str | None, str | None]:
        task_value = data.get("task")
        task: dict[str, Any] = task_value if isinstance(task_value, dict) else data
        raw_results = task.get("move_result") or data.get("move_result") or []
        if isinstance(raw_results, dict):
            results = [raw_results]
        elif isinstance(raw_results, list):
            results = [item for item in raw_results if isinstance(item, dict)]
        else:
            raise FeishuProtocolError("Wiki move task result is malformed")
        if not results:
            status = task.get("status")
            if status not in (
                None,
                0,
                1,
                "0",
                "1",
                "pending",
                "processing",
                "running",
            ):
                return None, str(task.get("status_msg") or status)
            return None, None
        result = results[0]
        status = result.get("status")
        if status in (1, "1", "pending", "processing", "running"):
            return None, None
        if status not in (None, 0, "0", "success"):
            return None, str(result.get("status_msg") or status)
        node_value = result.get("node")
        node: dict[str, Any] = node_value if isinstance(node_value, dict) else {}
        token = str(
            result.get("wiki_token")
            or result.get("node_token")
            or node.get("wiki_token")
            or node.get("node_token")
            or ""
        )
        return (token or None), None
