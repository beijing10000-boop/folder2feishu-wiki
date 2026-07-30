from __future__ import annotations

from datetime import UTC, datetime, timedelta

from folder2feishu.core import (
    ActionType,
    CoreStore,
    LedgerPersistenceHooks,
    MigrationState,
)
from folder2feishu.feishu.models import UploadSession


def test_persistence_hooks_commit_every_remote_checkpoint(tmp_path):
    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="hooks", source_root=tmp_path)
        action = store.save_plan(
            project.id,
            "plan",
            [
                {
                    "action_type": ActionType.UPLOAD,
                    "state": MigrationState.PLANNED,
                    "source_rel_path": "A/big.bin",
                    "destination_rel_path": "A/big.bin",
                }
            ],
        )[0]
        hooks = LedgerPersistenceHooks(
            store,
            project_id=project.id,
            planned_action_id=action.id,
            idempotency_key="deterministic-item-key",
        )
        expiry = datetime.now(UTC) + timedelta(hours=1)
        hooks.on_upload_session(
            UploadSession(
                upload_id="upload-id",
                parent_node="staging-folder",
                file_name="deterministic-name.bin",
                size=8,
                block_size=4,
                block_num=2,
                completed_parts=frozenset(),
                expires_at=expiry.timestamp(),
            )
        )
        hooks.on_upload_part("upload-id", 0)
        hooks.on_upload_part("upload-id", 0)
        hooks.on_file_token("file-token")
        hooks.on_wiki_task("task-token")
        hooks.on_wiki_token("wiki-token")

        checkpoint = store.get_plan_action(action.id)
        assert checkpoint.drive_file_token == "file-token"
        assert checkpoint.move_task_id == "task-token"
        assert checkpoint.wiki_node_token == "wiki-token"
        upload = store.get_upload_session_for_action(action.id)
        assert upload.completed_parts == [0]
        assert upload.drive_file_token == "file-token"
        assert upload.move_task_id == "task-token"
        assert upload.wiki_node_token == "wiki-token"
        assert hooks.resume_upload_session() is None  # upload already finished
    finally:
        store.close()


def test_direct_upload_token_is_durable_without_multipart_session(tmp_path):
    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="direct", source_root=tmp_path)
        action = store.save_plan(
            project.id,
            "plan",
            [
                {
                    "action_type": ActionType.UPLOAD,
                    "state": MigrationState.PLANNED,
                    "source_rel_path": "small.txt",
                    "destination_rel_path": "small.txt",
                }
            ],
        )[0]
        hooks = LedgerPersistenceHooks(
            store,
            project_id=project.id,
            planned_action_id=action.id,
            idempotency_key="small",
        )
        hooks.on_file_token("direct-token")
        assert store.get_plan_action(action.id).drive_file_token == "direct-token"
        assert store.get_upload_session_for_action(action.id) is None
    finally:
        store.close()
