from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class RetryMode(StrEnum):
    NEVER = "never"
    SAFE = "safe"
    SERVER = "server"
    ALWAYS = "always"
    RATE_LIMIT = "rate_limit"


@dataclass(frozen=True, slots=True)
class UploadSession:
    upload_id: str
    parent_node: str
    file_name: str
    size: int
    block_size: int
    block_num: int
    completed_parts: frozenset[int] = field(default_factory=frozenset)
    expires_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["completed_parts"] = sorted(self.completed_parts)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> UploadSession:
        return cls(
            upload_id=str(value["upload_id"]),
            parent_node=str(value["parent_node"]),
            file_name=str(value["file_name"]),
            size=int(value["size"]),
            block_size=int(value["block_size"]),
            block_num=int(value["block_num"]),
            completed_parts=frozenset(int(part) for part in value.get("completed_parts", ())),
            expires_at=float(value.get("expires_at") or 0),
        )


class PersistenceHooks(Protocol):
    """Synchronous durability boundary supplied by the migration ledger.

    Each callback must durably commit before returning. The services call these
    methods immediately after Feishu returns the corresponding remote ID and
    before performing the next remote operation.
    """

    def on_upload_session(self, session: UploadSession) -> None: ...

    def on_upload_part(self, upload_id: str, sequence: int) -> None: ...

    def on_upload_attempt(self) -> None: ...

    def on_file_token(self, file_token: str) -> None: ...

    def on_wiki_task(self, task_id: str) -> None: ...

    def on_wiki_token(self, wiki_token: str) -> None: ...


class NullPersistenceHooks:
    def on_upload_session(self, session: UploadSession) -> None:
        del session

    def on_upload_part(self, upload_id: str, sequence: int) -> None:
        del upload_id, sequence

    def on_upload_attempt(self) -> None:
        pass

    def on_file_token(self, file_token: str) -> None:
        del file_token

    def on_wiki_task(self, task_id: str) -> None:
        del task_id

    def on_wiki_token(self, wiki_token: str) -> None:
        del wiki_token


@dataclass(frozen=True, slots=True)
class StagingLocation:
    root_token: str
    project_token: str
    shard_token: str


@dataclass(frozen=True, slots=True)
class WikiReference:
    node_token: str
    host: str | None = None


@dataclass(frozen=True, slots=True)
class MoveSubmission:
    wiki_token: str | None = None
    task_id: str | None = None
    applied: bool = False


class ReconcileStatus(StrEnum):
    MATCH = "match"
    MISSING = "missing"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    status: ReconcileStatus
    node: dict[str, Any] | None = None
    differences: tuple[str, ...] = ()
