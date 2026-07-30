from __future__ import annotations

import hashlib

from folder2feishu.core import (
    CoreStore,
    InventoryScanner,
    IssueCode,
    ItemKind,
    file_attribute_flags,
)
from folder2feishu.core.scanner import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
)


def test_scanner_preserves_root_empty_directories_names_and_hashes(tmp_path):
    source = tmp_path / "Team FabDazzle - 文档"
    (source / "Apparel & Design" / "空目录").mkdir(parents=True)
    payload = "中文/emoji 😀".encode()
    (source / "Apparel & Design" / "原名 (最终).txt").write_bytes(payload)
    (source / "零字节.dat").write_bytes(b"")

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="JF", source_root=source)
        progress = []
        result = InventoryScanner(store, batch_size=2, progress_interval=1).scan(
            project.id, progress=progress.append
        )

        assert result.complete
        assert result.folders == 3  # root is a first-class folder
        assert result.files == 2
        assert result.bytes == len(payload)
        assert progress[-1].scanned_items == 5

        items = store.list_inventory(project.id, present=True)
        by_path = {item.rel_path: item for item in items}
        assert by_path[""].kind == ItemKind.FOLDER
        assert by_path["Apparel & Design/空目录"].kind == ItemKind.FOLDER
        file_item = by_path["Apparel & Design/原名 (最终).txt"]
        assert file_item.name == "原名 (最终).txt"
        assert file_item.sha256 == hashlib.sha256(payload).hexdigest()
        assert file_item.file_identity

        issues = store.list_issues(project.id, scan_id=result.scan_id)
        assert [issue.code for issue in issues] == [IssueCode.ZERO_BYTE_FILE]
        assert store.get_project(project.id).scan_complete
    finally:
        store.close()


def test_cancelled_scan_is_incomplete_and_audited(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(20):
        (source / f"{index:02}.txt").write_text(str(index), encoding="utf-8")

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="cancel", source_root=source)
        calls = 0

        def cancel() -> bool:
            nonlocal calls
            calls += 1
            return calls > 4

        result = InventoryScanner(store, batch_size=3).scan(project.id, cancel=cancel)
        assert result.cancelled
        assert not result.complete
        assert not store.get_project(project.id).scan_complete
        assert IssueCode.SCAN_CANCELLED in {issue.code for issue in store.list_issues(project.id)}
        assert store.list_audit(project.id)[-1].event_type == "scan.incomplete"
    finally:
        store.close()


def test_onedrive_attribute_detection():
    offline, recall_open, recall_data = file_attribute_flags(
        FILE_ATTRIBUTE_OFFLINE
        | FILE_ATTRIBUTE_RECALL_ON_OPEN
        | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    )
    assert offline
    assert recall_open
    assert recall_data
    assert file_attribute_flags(0) == (False, False, False)


def test_scanner_never_commits_more_than_configured_batch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(257):
        (source / f"{index:03}.txt").write_bytes(b"x")

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="batch", source_root=source)
        original = store.replace_inventory
        observed_batch_sizes = []

        def observed(project_id, scan_id, rows, *, mark_absent=True):
            buffered = list(rows)
            observed_batch_sizes.append(len(buffered))
            return original(project_id, scan_id, buffered, mark_absent=mark_absent)

        store.replace_inventory = observed
        result = InventoryScanner(store, batch_size=32).scan(project.id)
        assert result.complete
        assert max(observed_batch_sizes) <= 32
        assert len(observed_batch_sizes) > 2
    finally:
        store.close()
