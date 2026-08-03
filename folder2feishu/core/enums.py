"""Stable string enums used by the migration ledger.

The enum values are deliberately part of the persisted database contract.
Changing a value therefore requires a schema/data migration.
"""

from __future__ import annotations

from enum import Enum


class ValueEnum(str, Enum):
    """An enum whose database and JSON representation is its value."""


class ProjectStatus(ValueEnum):
    NEW = "NEW"
    SCANNED = "SCANNED"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"


class ItemKind(ValueEnum):
    FOLDER = "FOLDER"
    FILE = "FILE"


class InventoryState(ValueEnum):
    DISCOVERED = "DISCOVERED"
    MANUAL_ACTION = "MANUAL_ACTION"


class IssueSeverity(ValueEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


class IssueCode(ValueEnum):
    ENUMERATION_ERROR = "ENUMERATION_ERROR"
    STAT_ERROR = "STAT_ERROR"
    HASH_ERROR = "HASH_ERROR"
    SYMLINK_SKIPPED = "SYMLINK_SKIPPED"
    OFFLINE_PLACEHOLDER = "OFFLINE_PLACEHOLDER"
    ZERO_BYTE_FILE = "ZERO_BYTE_FILE"
    NAME_TOO_LONG = "NAME_TOO_LONG"
    WIKI_DEPTH_LIMIT = "WIKI_DEPTH_LIMIT"
    WIKI_CHILD_LIMIT = "WIKI_CHILD_LIMIT"
    SOURCE_ROOT_UNAVAILABLE = "SOURCE_ROOT_UNAVAILABLE"
    SCAN_CANCELLED = "SCAN_CANCELLED"


class ActionType(ValueEnum):
    CREATE_FOLDER = "CREATE_FOLDER"
    UPLOAD = "UPLOAD"
    SKIP = "SKIP"
    MOVE = "MOVE"
    RENAME = "RENAME"
    VERSION_UPDATE = "VERSION_UPDATE"
    REPORT_MISSING = "REPORT_MISSING"
    CONFLICT = "CONFLICT"
    MANUAL_ACTION = "MANUAL_ACTION"


class MigrationState(ValueEnum):
    DISCOVERED = "DISCOVERED"
    PLANNED = "PLANNED"
    UPLOADING = "UPLOADING"
    DRIVE_UPLOADED = "DRIVE_UPLOADED"
    WIKI_MOVING = "WIKI_MOVING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    PAUSED = "PAUSED"
    RETRYABLE = "RETRYABLE"
    CONFLICT = "CONFLICT"
    MANUAL_ACTION = "MANUAL_ACTION"


class RemoteStatus(ValueEnum):
    ACTIVE = "ACTIVE"
    UNVERIFIED = "UNVERIFIED"
    CONFLICT = "CONFLICT"
    MISSING = "MISSING"
    ARCHIVED = "ARCHIVED"


class UploadStatus(ValueEnum):
    PREPARED = "PREPARED"
    UPLOADING = "UPLOADING"
    FINISHED = "FINISHED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class RunType(ValueEnum):
    SCAN = "SCAN"
    PLAN = "PLAN"
    MIGRATION = "MIGRATION"
    RECONCILIATION = "RECONCILIATION"


class RunStatus(ValueEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    INTERRUPTED = "INTERRUPTED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AuditLevel(ValueEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
