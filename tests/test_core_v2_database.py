from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect

from folder2feishu.core import (
    ActionType,
    AuditLevel,
    CoreStore,
    ItemKind,
    LeaseBusyError,
    MigrationState,
    RemoteStatus,
    UploadStatus,
)

EXPECTED_TABLES = {
    "projects",
    "inventory_items",
    "scan_issues",
    "planned_actions",
    "remote_mappings",
    "upload_sessions",
    "job_runs",
    "audit_events",
    "job_leases",
    "schema_version",
}


def test_database_schema_pragmas_serialization_and_crud(tmp_path):
    store = CoreStore(tmp_path / "state" / "ledger.db", busy_timeout_ms=12_345)
    try:
        assert set(inspect(store.engine).get_table_names()) >= EXPECTED_TABLES
        pragmas = store.pragmas()
        assert pragmas["journal_mode"].lower() == "wal"
        assert pragmas["busy_timeout"] == 12_345
        assert pragmas["foreign_keys"] == 1

        project = store.create_project(
            name="JF 迁移",
            source_root=tmp_path / "Team FabDazzle - 文档",
            target_wiki_url="https://example.feishu.cn/wiki/token",
        )
        assert store.get_project(project.id).wrapper_name == "Team FabDazzle - 文档"
        assert store.list_projects()[0].id == project.id

        mapping = store.upsert_remote_mapping(
            project_id=project.id,
            item_kind=ItemKind.FILE,
            last_source_rel_path="设计/样衣.xlsx",
            source_file_identity="1:2",
            source_sha256="a" * 64,
            source_size=123,
            wiki_space_id="space",
            wiki_node_token="wiki",
            object_token="file",
            remote_parent_node_token="parent",
            remote_title="样衣.xlsx",
            remote_status=RemoteStatus.ACTIVE,
            last_verified_at=datetime.now(UTC),
        )
        assert store.get_remote_mapping(mapping.id).remote_title == "样衣.xlsx"
        assert store.get_remote_mapping(mapping.id).last_verified_at is not None
        encoded = store.serialize(mapping)
        assert encoded["item_kind"] == "FILE"
        assert encoded["created_at"].endswith("+00:00")

        event = store.append_audit(
            project.id,
            "test.event",
            "审计记录",
            level=AuditLevel.INFO,
            payload={"ok": True},
        )
        assert event.id > 0
        assert store.list_audit(project.id)[0].payload == {"ok": True}

        actions = store.save_plan(
            project.id,
            "plan-1",
            [
                {
                    "action_type": ActionType.UPLOAD,
                    "state": MigrationState.PLANNED,
                    "source_rel_path": "设计/样衣.xlsx",
                    "destination_rel_path": "设计/样衣.xlsx",
                }
            ],
        )
        action = store.update_plan_action(
            actions[0].id,
            state=MigrationState.UPLOADING,
            merge_details={"checkpoint": "prepared"},
        )
        assert store.get_plan_action(action.id).details["checkpoint"] == "prepared"

        upload = store.upsert_upload_session(
            project_id=project.id,
            planned_action_id=action.id,
            upload_id="upload-1",
            idempotency_key="idem",
            file_size=8,
            part_size=4,
            total_parts=2,
        )
        progress = store.record_upload_progress(
            action.id,
            completed_part=1,
            status=UploadStatus.UPLOADING,
            drive_file_token="file-token",
            move_task_id="task-id",
            increment_attempts=True,
        )
        progress = store.record_upload_progress(action.id, completed_part=1)
        assert progress.completed_parts == [1]
        assert progress.drive_file_token == "file-token"
        assert progress.move_task_id == "task-id"
        assert progress.attempts == 1
        assert store.get_upload_session_for_action(action.id).id == upload.id

        assert (
            store.find_current_remote_mapping(project.id, rel_path="设计/样衣.xlsx").id
            == mapping.id
        )
        store.mark_remote_mapping_historical(mapping.id)
        assert store.find_current_remote_mapping(project.id, rel_path="设计/样衣.xlsx") is None
    finally:
        store.close()


def test_job_lease_is_single_writer_and_expired_lease_can_be_taken(tmp_path):
    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="p", source_root=tmp_path)
        clock = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)
        first = store.acquire_lease(project.id, "migration", "worker-a", ttl_seconds=30, now=clock)
        assert first.owner_id == "worker-a"
        with pytest.raises(LeaseBusyError):
            store.acquire_lease(
                project.id,
                "migration",
                "worker-b",
                ttl_seconds=30,
                now=clock + timedelta(seconds=1),
            )
        replacement = store.acquire_lease(
            project.id,
            "migration",
            "worker-b",
            ttl_seconds=30,
            now=clock + timedelta(seconds=31),
        )
        assert replacement.owner_id == "worker-b"
        assert not store.release_lease(project.id, "migration", "worker-a")
        assert store.release_lease(project.id, "migration", "worker-b")
    finally:
        store.close()
