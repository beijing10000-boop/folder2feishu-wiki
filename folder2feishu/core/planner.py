"""Deterministic safe-incremental plan generation."""

from __future__ import annotations

import math
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .database import CoreStore
from .enums import (
    ActionType,
    AuditLevel,
    InventoryState,
    ItemKind,
    MigrationState,
    ProjectStatus,
    RemoteStatus,
    RunStatus,
    RunType,
)
from .models import InventoryItem, RemoteMapping, utc_now

DIRECT_UPLOAD_LIMIT = 20 * 1024 * 1024
CHUNK_SIZE = 4 * 1024 * 1024


class PlanBlockedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlanResult:
    project_id: str
    plan_id: str
    run_id: str
    scan_id: str
    total_actions: int
    counts: dict[str, int]
    estimated_upload_calls: int
    estimated_minimum_days: int
    blocked: bool


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else ""


def _relocation_type(previous: str, current: str) -> ActionType:
    if _parent(previous) == _parent(current):
        return ActionType.RENAME
    return ActionType.MOVE


def _upload_calls(size: int | None) -> int:
    if size == 0:
        return 0
    if size is None or size <= DIRECT_UPLOAD_LIMIT:
        return 1
    return 2 + math.ceil(size / CHUNK_SIZE)


class MigrationPlanner:
    """Compare current local facts with last verified remote mappings.

    Matching precedence is intentionally conservative:

    1. same source relative path;
    2. globally unique file identity in this project;
    3. globally unique SHA-256 in this project.

    Ambiguous identities/hashes never trigger a destructive remote move.
    """

    def __init__(self, store: CoreStore):
        self.store = store

    def estimate_pending_upload_bytes(self, project_id: str) -> int:
        """Return bytes that the current scan would actually upload.

        This is the read-only counterpart of :meth:`build`: it reuses the same
        path/File-ID/SHA matching and mapping classification, but does not
        create a plan, job run, audit event, or any other ledger row. Conflict
        and manual-action items deliberately contribute zero because execution
        is blocked until an operator resolves them.
        """

        project = self.store.get_project(project_id)
        if not project.current_scan_id or not project.scan_complete:
            raise PlanBlockedError("a complete local scan is required before estimating uploads")

        items = self.store.list_inventory(project_id, present=True)
        mappings = self.store.list_remote_mappings(project_id, current_only=True)
        matches, ambiguity = self._match(items, mappings)
        pending_bytes = 0

        for item in items:
            if item.kind != ItemKind.FILE:
                continue
            mapping = matches.get(item.id)
            if item.id in ambiguity:
                continue
            if mapping is not None and mapping.remote_status != RemoteStatus.ACTIVE:
                continue
            if item.state == InventoryState.MANUAL_ACTION:
                continue
            if mapping is None:
                pending_bytes += int(item.size or 0)
                continue
            if mapping.item_kind != item.kind:
                continue
            action = self._action_for_mapping(item, mapping)
            if action["action_type"] in {ActionType.UPLOAD, ActionType.VERSION_UPDATE}:
                pending_bytes += int(item.size or 0)
        return pending_bytes

    # Short alias used by preflight/orchestrator callers.
    pending_upload_bytes = estimate_pending_upload_bytes

    def build(self, project_id: str) -> PlanResult:
        project = self.store.get_project(project_id)
        if not project.current_scan_id or not project.scan_complete:
            raise PlanBlockedError("a complete local scan is required before building a plan")

        scan_id = project.current_scan_id
        plan_id = uuid.uuid4().hex
        run = self.store.create_job_run(
            project_id,
            RunType.PLAN,
            status=RunStatus.RUNNING,
            scan_id=scan_id,
            plan_id=plan_id,
        )
        items = self.store.list_inventory(project_id, present=True)
        mappings = self.store.list_remote_mappings(project_id, current_only=True)
        issues = self.store.list_issues(project_id, scan_id=scan_id)
        issues_by_path: dict[str, list[dict[str, str]]] = defaultdict(list)
        for issue in issues:
            issues_by_path[issue.rel_path].append(
                {
                    "code": issue.code.value,
                    "severity": issue.severity.value,
                    "message": issue.message,
                }
            )

        matches, ambiguity = self._match(items, mappings)
        matched_mapping_ids = {mapping.id for mapping in matches.values() if mapping is not None}
        actions: list[dict[str, Any]] = []

        for item in items:
            mapping = matches.get(item.id)
            item_issues = issues_by_path.get(item.rel_path, [])
            if item.id in ambiguity:
                actions.append(
                    self._action(
                        item,
                        None,
                        ActionType.CONFLICT,
                        "source identity is ambiguous; remote movement is disabled",
                        details={
                            "ambiguity": ambiguity[item.id],
                            "scan_issues": item_issues,
                        },
                    )
                )
                continue
            if mapping is not None and mapping.remote_status != RemoteStatus.ACTIVE:
                actions.append(
                    self._action(
                        item,
                        mapping,
                        ActionType.CONFLICT,
                        mapping.conflict_reason
                        or f"remote mapping status is {mapping.remote_status.value}",
                        details={"scan_issues": item_issues},
                    )
                )
                continue
            if item.state == InventoryState.MANUAL_ACTION:
                actions.append(
                    self._action(
                        item,
                        mapping,
                        ActionType.MANUAL_ACTION,
                        "preflight issue requires an explicit operator decision",
                        details={"scan_issues": item_issues},
                    )
                )
                continue
            if mapping is None:
                zero_byte = item.kind == ItemKind.FILE and item.size == 0
                action_type = (
                    ActionType.SKIP
                    if zero_byte
                    else ActionType.CREATE_FOLDER
                    if item.kind == ItemKind.FOLDER
                    else ActionType.UPLOAD
                )
                actions.append(
                    self._action(
                        item,
                        None,
                        action_type,
                        (
                            "Feishu does not support zero-byte files; report and skip"
                            if zero_byte
                            else "source item has no remote mapping"
                        ),
                        details=(
                            {"zero_byte_skipped": True, "source_size": 0} if zero_byte else None
                        ),
                    )
                )
                continue
            if mapping.item_kind != item.kind:
                actions.append(
                    self._action(
                        item,
                        mapping,
                        ActionType.CONFLICT,
                        "local item type differs from its remote mapping",
                    )
                )
                continue
            actions.append(self._action_for_mapping(item, mapping))

        # A current mapping that did not match this scan is never deleted or
        # moved automatically.  It becomes an explicit missing-source report.
        for mapping in mappings:
            if mapping.id in matched_mapping_ids:
                continue
            action_type = (
                ActionType.CONFLICT
                if mapping.remote_status != RemoteStatus.ACTIVE
                else ActionType.REPORT_MISSING
            )
            actions.append(
                {
                    "inventory_item_id": None,
                    "remote_mapping_id": mapping.id,
                    "action_type": action_type,
                    "state": (
                        MigrationState.CONFLICT
                        if action_type == ActionType.CONFLICT
                        else MigrationState.PLANNED
                    ),
                    "source_rel_path": "",
                    "previous_rel_path": mapping.last_source_rel_path,
                    "destination_rel_path": "",
                    "reason": (
                        mapping.conflict_reason
                        or "remote mapping has no corresponding local source item"
                    ),
                    "details": {
                        "safe_incremental": "report_only",
                        "remote_status": mapping.remote_status.value,
                    },
                }
            )

        actions.sort(key=self._sort_key)
        for index, action in enumerate(actions):
            action["order_index"] = index
        saved = self.store.save_plan(project_id, plan_id, actions)
        counts = Counter(action.action_type.value for action in saved)
        upload_item_ids = {
            action.inventory_item_id
            for action in saved
            if action.inventory_item_id
            and action.action_type in {ActionType.UPLOAD, ActionType.VERSION_UPDATE}
        }
        estimated_calls = sum(
            _upload_calls(item.size) for item in items if item.id in upload_item_ids
        )
        blocked = bool(counts[ActionType.CONFLICT.value] or counts[ActionType.MANUAL_ACTION.value])
        # The application does not impose a daily upload-call ceiling.  Keep
        # this compatibility field at zero instead of presenting an artificial
        # multi-day estimate; Feishu rate-limit backoff still applies at run time.
        minimum_days = 0
        summary = {
            "counts": dict(counts),
            "estimated_upload_calls": estimated_calls,
            "estimated_minimum_days": minimum_days,
            "blocked": blocked,
        }
        self.store.update_project(
            project_id,
            status=ProjectStatus.BLOCKED if blocked else ProjectStatus.PLANNED,
        )
        self.store.update_job_run(
            run.id,
            status=RunStatus.COMPLETE,
            total_items=len(saved),
            completed_items=len(saved),
            skipped_items=counts[ActionType.SKIP.value],
            summary=summary,
            finished_at=utc_now(),
        )
        self.store.append_audit(
            project_id,
            "plan.built",
            "safe incremental plan created",
            level=AuditLevel.WARNING if blocked else AuditLevel.INFO,
            job_run_id=run.id,
            payload={"plan_id": plan_id, **summary},
        )
        return PlanResult(
            project_id=project_id,
            plan_id=plan_id,
            run_id=run.id,
            scan_id=scan_id,
            total_actions=len(saved),
            counts=dict(counts),
            estimated_upload_calls=estimated_calls,
            estimated_minimum_days=minimum_days,
            blocked=blocked,
        )

    # Friendly alias for API/orchestrator code.
    build_plan = build

    @staticmethod
    def _match(
        items: list[InventoryItem], mappings: list[RemoteMapping]
    ) -> tuple[dict[str, RemoteMapping | None], dict[str, dict[str, Any]]]:
        matches: dict[str, RemoteMapping | None] = {item.id: None for item in items}
        ambiguity: dict[str, dict[str, Any]] = {}
        used: set[str] = set()

        by_path: dict[tuple[ItemKind, str], list[RemoteMapping]] = defaultdict(list)
        by_any_path: dict[str, list[RemoteMapping]] = defaultdict(list)
        for mapping in mappings:
            by_path[(mapping.item_kind, mapping.last_source_rel_path)].append(mapping)
            by_any_path[mapping.last_source_rel_path].append(mapping)
        for item in items:
            candidates = by_path.get((item.kind, item.rel_path), [])
            if len(candidates) == 1:
                matches[item.id] = candidates[0]
                used.add(candidates[0].id)
            elif len(candidates) > 1:
                ambiguity[item.id] = {
                    "method": "path",
                    "candidate_mapping_ids": [candidate.id for candidate in candidates],
                }
            else:
                wrong_type = by_any_path.get(item.rel_path, [])
                if len(wrong_type) == 1:
                    # Keep the association so the caller emits one explicit
                    # type CONFLICT rather than an unsafe create+missing pair.
                    matches[item.id] = wrong_type[0]
                    used.add(wrong_type[0].id)
                elif len(wrong_type) > 1:
                    ambiguity[item.id] = {
                        "method": "path_type_mismatch",
                        "candidate_mapping_ids": [candidate.id for candidate in wrong_type],
                    }

        MigrationPlanner._match_unique_key(
            items,
            mappings,
            matches,
            ambiguity,
            used,
            item_key=lambda item: item.file_identity,
            mapping_key=lambda mapping: mapping.source_file_identity,
            method="file_identity",
        )
        MigrationPlanner._match_unique_key(
            [item for item in items if item.kind == ItemKind.FILE],
            [mapping for mapping in mappings if mapping.item_kind == ItemKind.FILE],
            matches,
            ambiguity,
            used,
            item_key=lambda item: item.sha256,
            mapping_key=lambda mapping: mapping.source_sha256,
            method="sha256",
        )
        return matches, ambiguity

    @staticmethod
    def _match_unique_key(
        items: list[InventoryItem],
        mappings: list[RemoteMapping],
        matches: dict[str, RemoteMapping | None],
        ambiguity: dict[str, dict[str, Any]],
        used: set[str],
        *,
        item_key: Any,
        mapping_key: Any,
        method: str,
    ) -> None:
        remaining_items = [
            item
            for item in items
            if matches[item.id] is None and item.id not in ambiguity and item_key(item)
        ]
        remaining_mappings = [
            mapping for mapping in mappings if mapping.id not in used and mapping_key(mapping)
        ]
        item_groups: dict[str, list[InventoryItem]] = defaultdict(list)
        mapping_groups: dict[str, list[RemoteMapping]] = defaultdict(list)
        for item in remaining_items:
            item_groups[str(item_key(item))].append(item)
        for mapping in remaining_mappings:
            mapping_groups[str(mapping_key(mapping))].append(mapping)
        for key, grouped_items in item_groups.items():
            grouped_mappings = mapping_groups.get(key, [])
            if len(grouped_items) == len(grouped_mappings) == 1:
                item = grouped_items[0]
                mapping = grouped_mappings[0]
                matches[item.id] = mapping
                used.add(mapping.id)
            elif grouped_mappings:
                for item in grouped_items:
                    ambiguity[item.id] = {
                        "method": method,
                        "value": key,
                        "local_count": len(grouped_items),
                        "remote_count": len(grouped_mappings),
                        "candidate_mapping_ids": [mapping.id for mapping in grouped_mappings],
                    }

    @staticmethod
    def _action_for_mapping(item: InventoryItem, mapping: RemoteMapping) -> dict[str, Any]:
        path_changed = item.rel_path != mapping.last_source_rel_path
        details: dict[str, Any] = {
            "matched_by": (
                "path"
                if not path_changed
                else "file_identity"
                if item.file_identity and item.file_identity == mapping.source_file_identity
                else "sha256"
            )
        }
        if item.kind == ItemKind.FOLDER:
            if path_changed:
                action_type = _relocation_type(mapping.last_source_rel_path, item.rel_path)
                details["rename_during_move"] = _name(mapping.last_source_rel_path) != _name(
                    item.rel_path
                )
                details["remote_move_api"] = "wiki.move_node"
                if details["rename_during_move"] or action_type == ActionType.RENAME:
                    details["remote_rename_api"] = "wiki.rename_node"
                return MigrationPlanner._action(
                    item,
                    mapping,
                    action_type,
                    "folder path changed and identity matched",
                    details=details,
                )
            return MigrationPlanner._action(
                item,
                mapping,
                ActionType.SKIP,
                "folder mapping is unchanged",
                details=details,
            )

        if not mapping.source_sha256:
            return MigrationPlanner._action(
                item,
                mapping,
                ActionType.CONFLICT,
                "remote mapping has no source hash baseline",
                details=details,
            )
        if item.size == 0:
            details["zero_byte_skipped"] = True
            details["source_size"] = 0
            details["remote_left_unchanged"] = True
            return MigrationPlanner._action(
                item,
                mapping,
                ActionType.SKIP,
                "Feishu does not support zero-byte files; keep the existing remote node unchanged",
                details=details,
            )
        if item.sha256 != mapping.source_sha256:
            details["content_changed"] = True
            details["relocation_also_required"] = path_changed
            details["replacement_strategy"] = "archive_old_then_upload_new"
            return MigrationPlanner._action(
                item,
                mapping,
                ActionType.VERSION_UPDATE,
                "file content changed; preserve the previous Wiki node as history",
                details=details,
            )
        if path_changed:
            action_type = _relocation_type(mapping.last_source_rel_path, item.rel_path)
            details["rename_during_move"] = _name(mapping.last_source_rel_path) != _name(
                item.rel_path
            )
            details["remote_move_api"] = "wiki.move_node"
            if details["rename_during_move"] or action_type == ActionType.RENAME:
                # A raw file's title belongs to Drive, not Docx.
                details["remote_rename_api"] = "drive.rename_file"
            return MigrationPlanner._action(
                item,
                mapping,
                action_type,
                "file content is unchanged and its source path moved",
                details=details,
            )
        return MigrationPlanner._action(
            item,
            mapping,
            ActionType.SKIP,
            "path and SHA-256 are unchanged",
            details=details,
        )

    @staticmethod
    def _action(
        item: InventoryItem,
        mapping: RemoteMapping | None,
        action_type: ActionType,
        reason: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = {
            ActionType.CONFLICT: MigrationState.CONFLICT,
            ActionType.MANUAL_ACTION: MigrationState.MANUAL_ACTION,
        }.get(action_type, MigrationState.PLANNED)
        return {
            "inventory_item_id": item.id,
            "remote_mapping_id": mapping.id if mapping else None,
            "action_type": action_type,
            "state": state,
            "source_rel_path": item.rel_path,
            "previous_rel_path": mapping.last_source_rel_path if mapping else "",
            "destination_rel_path": item.rel_path,
            "reason": reason,
            "details": details or {},
        }

    @staticmethod
    def _sort_key(action: dict[str, Any]) -> tuple[int, int, str]:
        action_type = action["action_type"]
        path = action.get("destination_rel_path") or action.get("previous_rel_path", "")
        depth = len(PurePosixPath(path).parts) if path else 0
        priority = {
            ActionType.CREATE_FOLDER: 10,
            ActionType.MOVE: 20,
            ActionType.RENAME: 20,
            ActionType.SKIP: 30,
            ActionType.UPLOAD: 40,
            ActionType.VERSION_UPDATE: 40,
            ActionType.MANUAL_ACTION: 50,
            ActionType.CONFLICT: 60,
            ActionType.REPORT_MISSING: 70,
        }[action_type]
        # Create/move parents before children; missing reports are informational.
        return priority, depth, path.casefold()
