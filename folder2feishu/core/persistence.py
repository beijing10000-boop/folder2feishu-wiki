"""Durability adapter for Feishu upload and Wiki service callbacks."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from folder2feishu.feishu.models import UploadSession as FeishuUploadSession

from .database import CoreStore
from .enums import UploadStatus


class LedgerPersistenceHooks:
    """Commit every remote identifier before the next network operation.

    The adapter intentionally uses synchronous transactions because the Feishu
    service callback contract is a durability boundary: returning means a
    process restart can continue from that exact checkpoint.
    """

    def __init__(
        self,
        store: CoreStore,
        *,
        project_id: str,
        planned_action_id: str,
        idempotency_key: str,
        upload_attempt: Callable[[], None] | None = None,
    ):
        self.store = store
        self.project_id = project_id
        self.planned_action_id = planned_action_id
        self.idempotency_key = idempotency_key
        self._upload_attempt = upload_attempt

    def on_upload_session(self, session: FeishuUploadSession) -> None:
        expires_at = datetime.fromtimestamp(session.expires_at, UTC) if session.expires_at else None
        self.store.upsert_upload_session(
            project_id=self.project_id,
            planned_action_id=self.planned_action_id,
            upload_id=session.upload_id,
            idempotency_key=self.idempotency_key,
            staging_parent_token=session.parent_node,
            staging_name=session.file_name,
            file_size=session.size,
            part_size=session.block_size,
            total_parts=session.block_num,
            completed_parts=sorted(session.completed_parts),
            status=UploadStatus.PREPARED,
            expires_at=expires_at,
        )

    def on_upload_part(self, upload_id: str, sequence: int) -> None:
        upload = self.store.get_upload_session(upload_id)
        if upload is None or upload.planned_action_id != self.planned_action_id:
            raise RuntimeError("upload callback does not match the durable planned action")
        self.store.record_upload_progress(
            self.planned_action_id,
            completed_part=sequence,
            status=UploadStatus.UPLOADING,
        )

    def on_upload_attempt(self) -> None:
        if self._upload_attempt is not None:
            self._upload_attempt()

    def on_file_token(self, file_token: str) -> None:
        # Direct (<=20 MiB) uploads do not have an UploadSession, therefore the
        # planned action is the authoritative checkpoint for every upload.
        self.store.update_plan_action(
            self.planned_action_id,
            drive_file_token=file_token,
            merge_details={"upload_checkpoint": "drive_file_token"},
        )
        upload = self.store.get_upload_session_for_action(self.planned_action_id)
        if upload is not None:
            self.store.record_upload_progress(
                self.planned_action_id,
                status=UploadStatus.FINISHED,
                drive_file_token=file_token,
            )

    def on_wiki_task(self, task_id: str) -> None:
        self.store.update_plan_action(
            self.planned_action_id,
            move_task_id=task_id,
            merge_details={"wiki_checkpoint": "move_task"},
        )
        upload = self.store.get_upload_session_for_action(self.planned_action_id)
        if upload is not None:
            self.store.record_upload_progress(
                self.planned_action_id,
                move_task_id=task_id,
            )

    def on_wiki_token(self, wiki_token: str) -> None:
        self.store.update_plan_action(
            self.planned_action_id,
            wiki_node_token=wiki_token,
            merge_details={"wiki_checkpoint": "wiki_token"},
        )
        upload = self.store.get_upload_session_for_action(self.planned_action_id)
        if upload is not None:
            self.store.record_upload_progress(
                self.planned_action_id,
                wiki_node_token=wiki_token,
            )

    def resume_upload_session(self, *, now: datetime | None = None) -> FeishuUploadSession | None:
        upload = self.store.get_upload_session_for_action(self.planned_action_id)
        if upload is None or upload.status in {
            UploadStatus.EXPIRED,
            UploadStatus.FAILED,
            UploadStatus.FINISHED,
        }:
            return None
        current = now or datetime.now(UTC)
        if upload.expires_at is not None:
            expires = upload.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if current >= expires:
                self.store.record_upload_progress(
                    self.planned_action_id, status=UploadStatus.EXPIRED
                )
                return None
        return FeishuUploadSession(
            upload_id=upload.upload_id,
            parent_node=upload.staging_parent_token,
            file_name=upload.staging_name,
            size=upload.file_size,
            block_size=upload.part_size,
            block_num=upload.total_parts,
            completed_parts=frozenset(upload.completed_parts or []),
            expires_at=(
                upload.expires_at.replace(tzinfo=UTC).timestamp()
                if upload.expires_at and upload.expires_at.tzinfo is None
                else upload.expires_at.timestamp()
                if upload.expires_at
                else 0.0
            ),
        )
