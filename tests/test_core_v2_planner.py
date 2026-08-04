from __future__ import annotations

import pytest

import folder2feishu.core.scanner as scanner_module
from folder2feishu.core import (
    ActionType,
    CoreStore,
    InventoryScanner,
    ItemKind,
    MigrationPlanner,
    PlanBlockedError,
    RemoteStatus,
)


def add_verified_mappings(store, project_id, items):
    mappings = {}
    for index, item in enumerate(items):
        mapping = store.upsert_remote_mapping(
            project_id=project_id,
            inventory_item_id=item.id,
            item_kind=item.kind,
            last_source_rel_path=item.rel_path,
            source_file_identity=item.file_identity,
            source_sha256=item.sha256,
            source_size=item.size,
            wiki_space_id="space-1",
            wiki_node_token=f"wiki-{index}",
            object_token=f"obj-{index}",
            remote_parent_node_token="parent",
            remote_title=item.name,
            remote_status=RemoteStatus.ACTIVE,
        )
        mappings[item.rel_path] = mapping
    return mappings


def action_map(store, project_id, plan_id):
    return {
        (action.action_type, action.source_rel_path, action.previous_rel_path): action
        for action in store.list_plan_actions(project_id, plan_id=plan_id)
    }


def test_first_plan_creates_all_folders_and_uploads_files(tmp_path):
    source = tmp_path / "源目录"
    (source / "A" / "空").mkdir(parents=True)
    (source / "A" / "one.txt").write_text("one", encoding="utf-8")
    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="first", source_root=source)
        InventoryScanner(store).scan(project.id)
        planner = MigrationPlanner(store)
        assert planner.estimate_pending_upload_bytes(project.id) == 3
        assert store.list_plan_actions(project.id) == []
        result = planner.build(project.id)
        assert result.counts == {"CREATE_FOLDER": 3, "UPLOAD": 1}
        assert result.estimated_upload_calls == 1
        assert not result.blocked
        actions = store.list_plan_actions(project.id, plan_id=result.plan_id)
        assert [action.source_rel_path for action in actions[:3]] == ["", "A", "A/空"]
    finally:
        store.close()


def test_pending_upload_bytes_is_zero_for_unchanged_move_and_rename_and_counts_update(
    tmp_path,
):
    source = tmp_path / "source"
    (source / "A").mkdir(parents=True)
    (source / "A" / "same.txt").write_text("same", encoding="utf-8")
    (source / "A" / "change.txt").write_text("old", encoding="utf-8")
    (source / "A" / "rename.txt").write_text("rename", encoding="utf-8")
    (source / "A" / "move.txt").write_text("move", encoding="utf-8")

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="estimate", source_root=source)
        scanner = InventoryScanner(store)
        scanner.scan(project.id)
        add_verified_mappings(store, project.id, store.list_inventory(project.id, present=True))
        planner = MigrationPlanner(store)

        # A second identical inventory is a zero-byte incremental run even when
        # the tenant has little free space left.
        scanner.scan(project.id)
        assert planner.estimate_pending_upload_bytes(project.id) == 0
        assert store.list_plan_actions(project.id) == []

        changed = "new content is longer"
        (source / "A" / "change.txt").write_text(changed, encoding="utf-8")
        (source / "A" / "rename.txt").rename(source / "A" / "renamed.txt")
        (source / "B").mkdir()
        (source / "A" / "move.txt").rename(source / "B" / "move.txt")
        scanner.scan(project.id)

        assert planner.pending_upload_bytes(project.id) == len(changed.encode("utf-8"))
        assert store.list_plan_actions(project.id) == []
    finally:
        store.close()


def test_safe_incremental_classifies_skip_move_rename_update_missing_and_conflict(
    tmp_path,
):
    source = tmp_path / "source"
    (source / "A").mkdir(parents=True)
    files = {
        "same.txt": "same",
        "rename.txt": "rename",
        "move.txt": "move",
        "change.txt": "old",
        "missing.txt": "missing",
        "remote-conflict.txt": "conflict",
    }
    for name, content in files.items():
        (source / "A" / name).write_text(content, encoding="utf-8")

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="incremental", source_root=source)
        InventoryScanner(store).scan(project.id)
        initial_items = store.list_inventory(project.id, present=True)
        mappings = add_verified_mappings(store, project.id, initial_items)
        store.upsert_remote_mapping(
            id=mappings["A/remote-conflict.txt"].id,
            project_id=project.id,
            last_source_rel_path="A/remote-conflict.txt",
            remote_status=RemoteStatus.CONFLICT,
            conflict_reason="remote title was changed manually",
        )

        (source / "A" / "rename.txt").rename(source / "A" / "renamed.txt")
        (source / "B").mkdir()
        (source / "A" / "move.txt").rename(source / "B" / "move.txt")
        (source / "A" / "change.txt").write_text("new", encoding="utf-8")
        (source / "A" / "missing.txt").unlink()
        (source / "A" / "new.txt").write_text("new file", encoding="utf-8")
        InventoryScanner(store).scan(project.id)

        result = MigrationPlanner(store).build(project.id)
        actions = store.list_plan_actions(project.id, plan_id=result.plan_id)
        by_current = {action.source_rel_path: action for action in actions}
        assert by_current["A/same.txt"].action_type == ActionType.SKIP
        assert by_current["A/renamed.txt"].action_type == ActionType.RENAME
        assert by_current["A/renamed.txt"].previous_rel_path == "A/rename.txt"
        assert by_current["A/renamed.txt"].details["remote_rename_api"] == "drive.rename_file"
        assert by_current["B/move.txt"].action_type == ActionType.MOVE
        assert by_current["A/change.txt"].action_type == ActionType.VERSION_UPDATE
        assert by_current["A/new.txt"].action_type == ActionType.UPLOAD
        assert by_current["A/remote-conflict.txt"].action_type == ActionType.CONFLICT
        missing = [action for action in actions if action.action_type == ActionType.REPORT_MISSING]
        assert [action.previous_rel_path for action in missing] == ["A/missing.txt"]
        assert result.blocked  # conflict must be resolved before execution
    finally:
        store.close()


def test_unique_hash_fallback_and_ambiguous_hash_are_safe(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "original.txt").write_text("unique", encoding="utf-8")
    (source / "duplicate-a.txt").write_text("duplicate", encoding="utf-8")
    (source / "duplicate-b.txt").write_text("duplicate", encoding="utf-8")
    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="hash", source_root=source)
        InventoryScanner(store).scan(project.id)
        initial = store.list_inventory(project.id, present=True)
        mappings = add_verified_mappings(store, project.id, initial)
        for mapping in mappings.values():
            if mapping.item_kind == ItemKind.FILE:
                store.upsert_remote_mapping(
                    id=mapping.id,
                    project_id=project.id,
                    last_source_rel_path=mapping.last_source_rel_path,
                    source_file_identity=None,
                )

        (source / "original.txt").rename(source / "unique-renamed.txt")
        (source / "duplicate-a.txt").rename(source / "duplicate-one.txt")
        (source / "duplicate-b.txt").rename(source / "duplicate-two.txt")
        InventoryScanner(store).scan(project.id)
        result = MigrationPlanner(store).build(project.id)
        actions = store.list_plan_actions(project.id, plan_id=result.plan_id)
        by_path = {action.source_rel_path: action for action in actions}
        assert by_path["unique-renamed.txt"].action_type == ActionType.RENAME
        assert by_path["unique-renamed.txt"].details["matched_by"] == "sha256"
        assert by_path["duplicate-one.txt"].action_type == ActionType.CONFLICT
        assert by_path["duplicate-two.txt"].action_type == ActionType.CONFLICT
    finally:
        store.close()


def test_zero_byte_is_reported_and_skipped_without_blocking_plan(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "empty.bin").write_bytes(b"")
    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="manual", source_root=source)
        InventoryScanner(store).scan(project.id)
        result = MigrationPlanner(store).build(project.id)
        zero_byte = [
            action
            for action in store.list_plan_actions(project.id, plan_id=result.plan_id)
            if action.source_rel_path == "empty.bin"
        ][0]
        assert zero_byte.action_type == ActionType.SKIP
        assert zero_byte.details["zero_byte_skipped"] is True
        assert result.estimated_upload_calls == 0
        assert not result.blocked

        store.update_project(project.id, scan_complete=False)
        try:
            MigrationPlanner(store).build(project.id)
        except PlanBlockedError:
            pass
        else:
            raise AssertionError("incomplete scan unexpectedly produced a plan")
    finally:
        store.close()


def test_offline_folder_preserves_existing_remote_descendants(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "source"
    (source / "cloud" / "nested").mkdir(parents=True)
    (source / "cloud" / "nested" / "file.txt").write_text("data", encoding="utf-8")
    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="offline-folder", source_root=source)
        scanner = InventoryScanner(store)
        scanner.scan(project.id)
        add_verified_mappings(store, project.id, store.list_inventory(project.id, present=True))

        calls = 0

        def cloud_folder_is_offline(_: int) -> tuple[bool, bool, bool]:
            nonlocal calls
            calls += 1
            return (True, False, False) if calls == 2 else (False, False, False)

        monkeypatch.setattr(scanner_module, "file_attribute_flags", cloud_folder_is_offline)
        scanner.scan(project.id)
        result = MigrationPlanner(store).build(project.id)
        actions = store.list_plan_actions(project.id, plan_id=result.plan_id)
        descendants = [
            action for action in actions if action.previous_rel_path.startswith("cloud/")
        ]
        assert descendants
        assert {action.action_type for action in descendants} == {ActionType.SKIP}
        assert all(action.details["placeholder_deferred"] is True for action in descendants)
        assert not result.blocked
    finally:
        store.close()
