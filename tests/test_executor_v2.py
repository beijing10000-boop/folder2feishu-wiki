from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from folder2feishu.core import (
    ActionType,
    CoreStore,
    InventoryScanner,
    LeaseBusyError,
    MigrationPlanner,
    MigrationState,
    RemoteStatus,
    RunStatus,
    RunType,
)
from folder2feishu.executor import (
    MigrationBlocked,
    MigrationExecutor,
    RemoteReconciler,
    _ProjectLeaseHeartbeat,
)
from folder2feishu.feishu import (
    ReconcileResult,
    ReconcileStatus,
    StagedFile,
    StagingLocation,
    deterministic_staging_name,
)
from folder2feishu.quota import DailyQuotaStore


class FakeDrive:
    def __init__(self) -> None:
        self.uploads = 0
        self.current_user_id = "fixed-user"
        self.names: dict[str, str] = {}
        self.renames: list[tuple[str, str]] = []
        self.item_keys: list[str] = []
        self.wiki_nodes: dict[str, dict[str, str]] | None = None

    def get_current_user_info(self) -> dict[str, str]:
        return {"user_id": self.current_user_id}

    def ensure_staging(self, project_id: str, *, shard_index: int = 0) -> StagingLocation:
        del project_id
        return StagingLocation("staging-root", "staging-project", f"shard-{shard_index}")

    def stage_file(
        self,
        local_path: Path,
        *,
        parent_node: str,
        project_id: str,
        item_key: str,
        original_name: str | None = None,
        resume_session: Any = None,
        hooks: Any = None,
    ) -> StagedFile:
        del parent_node, project_id, resume_session
        self.item_keys.append(item_key)
        self.uploads += 1
        token = f"file-{self.uploads}"
        name = original_name or local_path.name
        self.names[token] = name
        hooks.on_file_token(token)
        return StagedFile(token, f"internal-{token}", name)

    def rename_file(self, file_token: str, new_title: str, *, object_type: str = "file") -> str:
        assert object_type == "file"
        self.renames.append((file_token, new_title))
        self.names[file_token] = new_title
        if self.wiki_nodes is not None:
            for node in self.wiki_nodes.values():
                if node.get("obj_token") == file_token:
                    node["title"] = new_title
        return file_token


class FakeWiki:
    def __init__(self, drive: FakeDrive) -> None:
        self.drive = drive
        self.nodes: dict[str, dict[str, str]] = {
            "target-parent": {
                "node_token": "target-parent",
                "space_id": "space-1",
                "parent_node_token": "",
                "title": "Pilot",
                "obj_token": "target-object",
            }
        }
        drive.wiki_nodes = self.nodes
        self.created = 0
        self.moves_to_wiki = 0
        self.reconcile_calls = 0

    def ensure_docx_node(self, space_id: str, title: str, parent_node_token: str) -> dict[str, str]:
        for node in self.nodes.values():
            if (
                node["space_id"] == space_id
                and node["title"] == title
                and node["parent_node_token"] == parent_node_token
            ):
                return node
        self.created += 1
        token = f"wiki-folder-{self.created}"
        node = {
            "node_token": token,
            "space_id": space_id,
            "parent_node_token": parent_node_token,
            "title": title,
            "obj_token": f"docx-{self.created}",
        }
        self.nodes[token] = node
        return node

    def move_file_to_wiki(
        self,
        space_id: str,
        *,
        file_token: str,
        parent_wiki_token: str,
        hooks: Any = None,
        timeout_seconds: float = 180,
        existing_task_id: str | None = None,
    ) -> str:
        del timeout_seconds, existing_task_id
        self.moves_to_wiki += 1
        task = f"task-{self.moves_to_wiki}"
        token = f"wiki-file-{self.moves_to_wiki}"
        hooks.on_wiki_task(task)
        self.nodes[token] = {
            "node_token": token,
            "space_id": space_id,
            "parent_node_token": parent_wiki_token,
            "title": self.drive.names[file_token],
            "obj_token": file_token,
        }
        hooks.on_wiki_token(token)
        return token

    def reconcile_node(
        self,
        node_token: str,
        *,
        expected_space_id: str | None = None,
        expected_parent_token: str | None = None,
        expected_title: str | None = None,
        expected_obj_token: str | None = None,
    ) -> ReconcileResult:
        self.reconcile_calls += 1
        node = self.nodes.get(node_token)
        if node is None:
            return ReconcileResult(ReconcileStatus.MISSING)
        differences: list[str] = []
        for field, expected in (
            ("space_id", expected_space_id),
            ("parent_node_token", expected_parent_token),
            ("title", expected_title),
            ("obj_token", expected_obj_token),
        ):
            if expected is not None and node.get(field) != expected:
                differences.append(field)
        return ReconcileResult(
            ReconcileStatus.CONFLICT if differences else ReconcileStatus.MATCH,
            node,
            tuple(differences),
        )

    def move_node(
        self,
        space_id: str,
        node_token: str,
        *,
        target_parent_token: str,
        target_space_id: str | None = None,
    ) -> str:
        node = self.nodes[node_token]
        node["space_id"] = target_space_id or space_id
        node["parent_node_token"] = target_parent_token
        return node_token

    def archive_file_node(
        self,
        space_id: str,
        node_token: str,
        *,
        history_parent_token: str,
    ) -> str:
        return self.move_node(space_id, node_token, target_parent_token=history_parent_token)

    def rename_node(self, space_id: str, node_token: str, title: str) -> str:
        del space_id
        self.nodes[node_token]["title"] = title
        return node_token


def _confirm(store: CoreStore, project_id: str) -> None:
    for action in store.list_plan_actions(project_id):
        store.update_plan_action(action.id, merge_details={"plan_confirmed": True})


def _build(tmp_path: Path) -> tuple[CoreStore, str, FakeDrive, FakeWiki]:
    source = tmp_path / "JF 原目录"
    (source / "空目录").mkdir(parents=True)
    (source / "部门 A" / "三级").mkdir(parents=True)
    (source / "部门 A" / "中文 & report (1).xlsx").write_bytes(b"first")
    (source / "部门 A" / "三级" / "deck.pptx").write_bytes(b"slides")
    store = CoreStore(tmp_path / "ledger.sqlite3")
    project = store.create_project(
        name="pilot",
        source_root=source,
        target_wiki_url="https://example.feishu.cn/wiki/target-parent",
        target_space_id="space-1",
        target_parent_node_token="target-parent",
        identity_key="fixed-user",
    )
    InventoryScanner(store).scan(project.id)
    MigrationPlanner(store).build(project.id)
    _confirm(store, project.id)
    drive = FakeDrive()
    wiki = FakeWiki(drive)
    return store, project.id, drive, wiki


def test_first_run_second_run_move_and_version_update(tmp_path: Path) -> None:
    store, project_id, drive, wiki = _build(tmp_path)
    executor = MigrationExecutor(
        store,
        drive,  # type: ignore[arg-type]
        wiki,  # type: ignore[arg-type]
        DailyQuotaStore(tmp_path / "quota.json"),
    )

    first = executor.execute(project_id)
    assert first.failed == first.conflicts == 0
    assert drive.uploads == 2
    assert all(
        mapping.remote_status == RemoteStatus.ACTIVE
        for mapping in store.list_remote_mappings(project_id)
    )

    InventoryScanner(store).scan(project_id)
    second_plan = MigrationPlanner(store).build(project_id)
    assert second_plan.counts[ActionType.SKIP.value] == second_plan.total_actions
    _confirm(store, project_id)
    executor.execute(project_id)
    assert drive.uploads == 2

    source = Path(store.get_project(project_id).source_root)
    old = source / "部门 A" / "三级" / "deck.pptx"
    moved = source / "部门 A" / "deck-renamed.pptx"
    old.rename(moved)
    InventoryScanner(store).scan(project_id)
    moved_plan = MigrationPlanner(store).build(project_id)
    assert (
        moved_plan.counts.get(ActionType.MOVE.value, 0)
        + moved_plan.counts.get(ActionType.RENAME.value, 0)
        == 1
    )
    _confirm(store, project_id)
    executor.execute(project_id)
    assert drive.uploads == 2

    changed = source / "部门 A" / "中文 & report (1).xlsx"
    changed.write_bytes(b"second-version")
    InventoryScanner(store).scan(project_id)
    version_plan = MigrationPlanner(store).build(project_id)
    assert version_plan.counts[ActionType.VERSION_UPDATE.value] == 1
    _confirm(store, project_id)
    executor.execute(project_id)
    assert drive.uploads == 3
    assert any(
        mapping.remote_status == RemoteStatus.ARCHIVED
        for mapping in store.list_remote_mappings(project_id, current_only=False)
    )


def test_zero_byte_file_is_reported_and_skipped_without_remote_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "zero-byte-source"
    source.mkdir()
    empty = source / "保留原名.empty"
    empty.write_bytes(b"")
    store = CoreStore(tmp_path / "zero-byte-ledger.sqlite3")
    try:
        project = store.create_project(
            name="zero-byte",
            source_root=source,
            target_wiki_url="https://example.feishu.cn/wiki/target-parent",
            target_space_id="space-1",
            target_parent_node_token="target-parent",
            identity_key="fixed-user",
        )
        InventoryScanner(store).scan(project.id)
        plan = MigrationPlanner(store).build(project.id)
        assert plan.counts[ActionType.SKIP.value] == 1
        assert plan.counts[ActionType.CREATE_FOLDER.value] == 1
        assert plan.estimated_upload_calls == 0
        _confirm(store, project.id)

        drive = FakeDrive()
        wiki = FakeWiki(drive)
        executor = MigrationExecutor(
            store,
            drive,  # type: ignore[arg-type]
            wiki,  # type: ignore[arg-type]
            DailyQuotaStore(tmp_path / "zero-byte-quota.json"),
        )
        first = executor.execute(project.id)
        assert first.failed == first.conflicts == 0
        assert drive.uploads == 0
        assert [
            mapping.last_source_rel_path
            for mapping in store.list_remote_mappings(project.id)
        ] == [""]
        zero_action = next(
            action
            for action in store.list_plan_actions(project.id)
            if action.source_rel_path == "保留原名.empty"
        )
        assert zero_action.state == MigrationState.DONE
        assert zero_action.details["zero_byte_skipped"] is True

        InventoryScanner(store).scan(project.id)
        unchanged = MigrationPlanner(store).build(project.id)
        assert unchanged.counts[ActionType.SKIP.value] == unchanged.total_actions
        _confirm(store, project.id)
        executor.execute(project.id)
        assert drive.uploads == 0

    finally:
        store.close()


def test_file_changed_to_zero_bytes_leaves_existing_remote_node_unchanged(
    tmp_path: Path,
) -> None:
    store, project_id, drive, wiki = _build(tmp_path)
    executor = MigrationExecutor(
        store,
        drive,  # type: ignore[arg-type]
        wiki,  # type: ignore[arg-type]
        DailyQuotaStore(tmp_path / "zero-transition-quota.json"),
    )
    executor.execute(project_id)
    before = next(
        mapping
        for mapping in store.list_remote_mappings(project_id)
        if mapping.last_source_rel_path == "部门 A/中文 & report (1).xlsx"
    )

    source = Path(store.get_project(project_id).source_root)
    (source / "部门 A" / "中文 & report (1).xlsx").write_bytes(b"")
    InventoryScanner(store).scan(project_id)
    plan = MigrationPlanner(store).build(project_id)
    zero_action = next(
        action
        for action in store.list_plan_actions(project_id, plan_id=plan.plan_id)
        if action.source_rel_path == "部门 A/中文 & report (1).xlsx"
    )
    assert zero_action.action_type == ActionType.SKIP
    assert zero_action.details["remote_left_unchanged"] is True
    _confirm(store, project_id)
    executor.execute(project_id)

    after = store.get_remote_mapping(before.id)
    assert drive.uploads == 2
    assert after.source_size == before.source_size
    assert after.source_sha256 == before.source_sha256


def test_resume_from_persisted_drive_token_does_not_reupload(
    tmp_path: Path,
) -> None:
    store, project_id, drive, wiki = _build(tmp_path)
    upload_action = next(
        action
        for action in store.list_plan_actions(project_id)
        if action.action_type == ActionType.UPLOAD
    )
    item = next(
        item
        for item in store.list_inventory(project_id)
        if item.id == upload_action.inventory_item_id
    )
    drive.names["recovered-file-token"] = deterministic_staging_name(
        project_id,
        item.id,
        item.name,
    )
    store.update_plan_action(
        upload_action.id,
        drive_file_token="recovered-file-token",
    )
    executor = MigrationExecutor(
        store,
        drive,  # type: ignore[arg-type]
        wiki,  # type: ignore[arg-type]
        DailyQuotaStore(tmp_path / "quota.json"),
    )
    result = executor.execute(project_id)
    assert result.failed == 0
    assert drive.uploads == 1
    assert ("recovered-file-token", item.name) in drive.renames
    recovered = store.get_plan_action(upload_action.id)
    assert recovered.wiki_node_token
    assert recovered.details["staging_name_restored"] is True


def test_durable_run_executes_its_plan_not_a_newer_unconfirmed_plan(tmp_path: Path) -> None:
    store, project_id, drive, wiki = _build(tmp_path)
    old_actions = store.list_plan_actions(project_id)
    old_plan_id = old_actions[0].plan_id
    run = store.create_job_run(
        project_id,
        RunType.MIGRATION,
        status=RunStatus.PAUSED,
        scan_id=store.get_project(project_id).current_scan_id,
        plan_id=old_plan_id,
    )

    InventoryScanner(store).scan(project_id)
    newer = MigrationPlanner(store).build(project_id)
    assert newer.plan_id != old_plan_id
    assert not any(
        (action.details or {}).get("plan_confirmed")
        for action in store.list_plan_actions(project_id, plan_id=newer.plan_id)
    )

    result = MigrationExecutor(
        store,
        drive,  # type: ignore[arg-type]
        wiki,  # type: ignore[arg-type]
        DailyQuotaStore(tmp_path / "quota.json"),
    ).execute(project_id, run_id=run.id)

    assert result.failed == result.conflicts == 0
    assert drive.uploads == 2
    assert all(
        action.state == MigrationState.DONE
        for action in store.list_plan_actions(project_id, plan_id=old_plan_id)
    )
    assert all(
        action.state != MigrationState.DONE
        for action in store.list_plan_actions(project_id, plan_id=newer.plan_id)
    )


def test_executor_blocks_changed_oauth_user_before_remote_write(tmp_path: Path) -> None:
    store, project_id, drive, wiki = _build(tmp_path)
    drive.current_user_id = "different-user"

    with pytest.raises(MigrationBlocked, match="OAuth 用户与项目绑定身份不一致"):
        MigrationExecutor(
            store,
            drive,  # type: ignore[arg-type]
            wiki,  # type: ignore[arg-type]
            DailyQuotaStore(tmp_path / "identity-quota.json"),
        ).execute(project_id)

    assert drive.uploads == 0
    assert drive.renames == []
    assert wiki.created == 0
    assert wiki.moves_to_wiki == 0
    assert wiki.reconcile_calls == 0


def test_same_content_files_use_distinct_migration_item_keys(tmp_path: Path) -> None:
    source = tmp_path / "same-content"
    (source / "A").mkdir(parents=True)
    (source / "B").mkdir(parents=True)
    (source / "A" / "same.txt").write_bytes(b"identical")
    (source / "B" / "same.txt").write_bytes(b"identical")
    store = CoreStore(tmp_path / "same-content.db")
    project = store.create_project(
        name="same-content",
        source_root=source,
        target_wiki_url="https://example.feishu.cn/wiki/target-parent",
        target_space_id="space-1",
        target_parent_node_token="target-parent",
        identity_key="fixed-user",
    )
    InventoryScanner(store).scan(project.id)
    MigrationPlanner(store).build(project.id)
    _confirm(store, project.id)
    drive = FakeDrive()
    wiki = FakeWiki(drive)

    result = MigrationExecutor(
        store,
        drive,  # type: ignore[arg-type]
        wiki,  # type: ignore[arg-type]
        DailyQuotaStore(tmp_path / "same-content-quota.json"),
    ).execute(project.id)

    assert result.failed == result.conflicts == 0
    assert len(drive.item_keys) == 2
    assert len(set(drive.item_keys)) == 2


def test_project_lease_heartbeat_refreshes_during_one_long_action() -> None:
    refreshed = threading.Event()

    class RecordingStore:
        def __init__(self) -> None:
            self.acquires = 0
            self.releases = 0

        def acquire_lease(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            self.acquires += 1
            if self.acquires >= 2:
                refreshed.set()

        def release_lease(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            self.releases += 1

    recording = RecordingStore()
    heartbeat = _ProjectLeaseHeartbeat(  # type: ignore[arg-type]
        recording,
        "project",
        "owner",
        ttl_seconds=1,
        interval_seconds=0.01,
    )
    heartbeat.start()
    try:
        assert refreshed.wait(timeout=1)
        heartbeat.checkpoint()
    finally:
        heartbeat.close()

    assert recording.acquires >= 2
    assert recording.releases == 1


def test_reconciler_uses_same_project_writer_lease(tmp_path: Path) -> None:
    store, project_id, drive, wiki = _build(tmp_path)
    MigrationExecutor(
        store,
        drive,  # type: ignore[arg-type]
        wiki,  # type: ignore[arg-type]
        DailyQuotaStore(tmp_path / "quota.json"),
    ).execute(project_id)
    store.acquire_lease(project_id, "migration", "other-process", ttl_seconds=60)

    with pytest.raises(LeaseBusyError):
        RemoteReconciler(
            store,
            drive,  # type: ignore[arg-type]
            wiki,  # type: ignore[arg-type]
        ).reconcile(project_id)


def test_reconciler_blocks_changed_oauth_user_before_remote_readback(
    tmp_path: Path,
) -> None:
    store, project_id, drive, wiki = _build(tmp_path)
    MigrationExecutor(
        store,
        drive,  # type: ignore[arg-type]
        wiki,  # type: ignore[arg-type]
        DailyQuotaStore(tmp_path / "identity-reconcile-quota.json"),
    ).execute(project_id)
    calls_before = wiki.reconcile_calls
    drive.current_user_id = "different-user"

    with pytest.raises(MigrationBlocked, match="OAuth 用户与项目绑定身份不一致"):
        RemoteReconciler(
            store,
            drive,  # type: ignore[arg-type]
            wiki,  # type: ignore[arg-type]
        ).reconcile(project_id)

    assert wiki.reconcile_calls == calls_before
