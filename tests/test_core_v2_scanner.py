from __future__ import annotations

import hashlib
import threading
import time

from folder2feishu.core import (
    ActionType,
    CoreStore,
    InventoryScanner,
    InventoryState,
    IssueCode,
    IssueSeverity,
    ItemKind,
    MigrationPlanner,
    file_attribute_flags,
)
from folder2feishu.core import scanner as scanner_module
from folder2feishu.core.scanner import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
)


def _nested_directory(root, depth: int):
    current = root
    for index in range(1, depth + 1):
        current = current / f"level-{index:02}"
    current.mkdir(parents=True)
    return current


def test_scanner_reserves_one_drive_level_for_project_wrapper(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _nested_directory(source, 14)

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="depth-14", source_root=source)
        result = InventoryScanner(store).scan(project.id)

        depth_issues = [
            issue
            for issue in store.list_issues(project.id, scan_id=result.scan_id)
            if issue.code == IssueCode.WIKI_DEPTH_LIMIT
        ]
        assert not depth_issues
    finally:
        store.close()


def test_scanner_blocks_local_depth_fifteen_before_preflight(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _nested_directory(source, 15)

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="depth-15", source_root=source)
        result = InventoryScanner(store).scan(project.id)

        depth_issues = [
            issue
            for issue in store.list_issues(project.id, scan_id=result.scan_id)
            if issue.code == IssueCode.WIKI_DEPTH_LIMIT
        ]
        assert len(depth_issues) == 1
        assert depth_issues[0].severity == IssueSeverity.BLOCKING
        assert depth_issues[0].details == {"depth": 15, "safe_local_limit": 14}
    finally:
        store.close()


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
        assert by_path["零字节.dat"].state == InventoryState.DISCOVERED

        issues = store.list_issues(project.id, scan_id=result.scan_id)
        assert [issue.code for issue in issues] == [IssueCode.ZERO_BYTE_FILE]
        assert issues[0].severity == IssueSeverity.WARNING
        assert issues[0].details["migration_policy"] == "report_and_skip"
        assert store.get_project(project.id).scan_complete
    finally:
        store.close()


def test_scanner_ignores_onedrive_internal_sync_marker(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".849C9593-D756-4E56-8D6E-42412F2A707B").write_bytes(b"OneDrive internal")
    (source / "business.txt").write_bytes(b"business")

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="OneDrive", source_root=source)
        result = InventoryScanner(store).scan(project.id)

        assert result.complete
        assert result.files == 1
        items = store.list_inventory(project.id, present=True)
        assert {item.rel_path for item in items} == {"", "business.txt"}
        assert not store.list_issues(project.id, scan_id=result.scan_id)
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


def test_placeholder_is_deferred_then_becomes_upload_after_hydration(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "later.docx").write_bytes(b"downloaded-content")
    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="deferred", source_root=source)
        flag_calls = 0

        def placeholder_on_file(_: int) -> tuple[bool, bool, bool]:
            nonlocal flag_calls
            flag_calls += 1
            return (False, False, False) if flag_calls == 1 else (True, False, False)

        monkeypatch.setattr(scanner_module, "file_attribute_flags", placeholder_on_file)
        first_scan = InventoryScanner(store).scan(project.id)
        assert first_scan.complete
        assert first_scan.blocking_issues == 0
        issues = store.list_issues(project.id, scan_id=first_scan.scan_id)
        assert len(issues) == 1
        assert issues[0].code == IssueCode.OFFLINE_PLACEHOLDER
        assert issues[0].severity == IssueSeverity.WARNING
        assert issues[0].details["migration_policy"] == "defer_until_next_scan"
        placeholder = next(
            item for item in store.list_inventory(project.id) if item.rel_path == "later.docx"
        )
        assert placeholder.state == InventoryState.DISCOVERED
        assert placeholder.sha256 is None

        first_plan = MigrationPlanner(store).build(project.id)
        deferred = next(
            action
            for action in store.list_plan_actions(project.id, plan_id=first_plan.plan_id)
            if action.source_rel_path == "later.docx"
        )
        assert first_plan.blocked is False
        assert deferred.action_type == ActionType.SKIP
        assert deferred.details["placeholder_deferred"] is True

        monkeypatch.setattr(
            scanner_module,
            "file_attribute_flags",
            lambda _: (False, False, False),
        )
        second_scan = InventoryScanner(store).scan(project.id)
        assert second_scan.complete
        hydrated = next(
            item for item in store.list_inventory(project.id) if item.rel_path == "later.docx"
        )
        assert hydrated.sha256 == hashlib.sha256(b"downloaded-content").hexdigest()

        second_plan = MigrationPlanner(store).build(project.id)
        upload = next(
            action
            for action in store.list_plan_actions(project.id, plan_id=second_plan.plan_id)
            if action.source_rel_path == "later.docx"
        )
        assert upload.action_type == ActionType.UPLOAD
        assert second_plan.blocked is False
    finally:
        store.close()


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


def test_unchanged_second_scan_reuses_previous_hashes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_bytes(b"one")
    (source / "two.txt").write_bytes(b"two")

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="incremental", source_root=source)
        first = InventoryScanner(store, hash_workers=2).scan(project.id)
        assert first.complete

        def unexpected_hash(*args, **kwargs):
            raise AssertionError("unchanged files must reuse their durable SHA-256")

        monkeypatch.setattr(scanner_module, "sha256_file", unexpected_hash)
        second = InventoryScanner(store, hash_workers=2).scan(project.id)

        assert second.complete
        summary = store.get_job_run(second.run_id).summary
        assert summary["hashes_reused"] == 2
        assert summary["hashes_computed"] == 0
    finally:
        store.close()


def test_fast_initial_scan_defers_new_file_hashes_until_migration(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_bytes(b"one")
    (source / "two.txt").write_bytes(b"two")

    def unexpected_hash(*args, **kwargs):
        raise AssertionError("fast initial inventory must not read entire file contents")

    monkeypatch.setattr(scanner_module, "sha256_file", unexpected_hash)
    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="fast-initial", source_root=source)
        scanner = InventoryScanner(store, defer_new_hashes=True, hash_workers=2)

        first = scanner.scan(project.id)
        assert first.complete
        first_summary = store.get_job_run(first.run_id).summary
        assert first_summary["hashes_deferred"] == 2
        assert first_summary["hashes_computed"] == 0
        files = store.list_inventory(project.id, kind=ItemKind.FILE, present=True)
        assert len(files) == 2
        assert all(item.sha256 is None for item in files)
        assert all(item.state == InventoryState.DISCOVERED for item in files)

        plan = MigrationPlanner(store).build(project.id)
        assert not plan.blocked
        assert {
            action.action_type
            for action in store.list_plan_actions(project.id, plan_id=plan.plan_id)
            if action.source_rel_path
        } == {ActionType.UPLOAD}

        # Restarting or rescanning before migration must keep the quick
        # fingerprint without falling back to hashing every file.
        second = scanner.scan(project.id)
        assert second.complete
        second_summary = store.get_job_run(second.run_id).summary
        assert second_summary["hashes_deferred"] == 2
        assert second_summary["hashes_computed"] == 0
    finally:
        store.close()


def test_changed_file_is_rehashed_while_unchanged_file_is_reused(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    changed = source / "changed.txt"
    unchanged = source / "unchanged.txt"
    changed.write_bytes(b"before")
    unchanged.write_bytes(b"stable")

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="incremental", source_root=source)
        first = InventoryScanner(store, hash_workers=2).scan(project.id)
        assert first.complete

        changed.write_bytes(b"after-with-a-different-size")
        original_hash = scanner_module.sha256_file
        hashed_paths = []

        def observed_hash(path, **kwargs):
            hashed_paths.append(path.name)
            return original_hash(path, **kwargs)

        monkeypatch.setattr(scanner_module, "sha256_file", observed_hash)
        second = InventoryScanner(store, hash_workers=2).scan(project.id)

        assert second.complete
        assert hashed_paths == ["changed.txt"]
        summary = store.get_job_run(second.run_id).summary
        assert summary["hashes_reused"] == 1
        assert summary["hashes_computed"] == 1
    finally:
        store.close()


def test_initial_scan_hashes_files_with_bounded_parallel_workers(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(8):
        folder = source / f"folder-{index}"
        folder.mkdir()
        (folder / f"{index}.txt").write_bytes(f"payload-{index}".encode())

    original_hash = scanner_module.sha256_file
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def observed_hash(path, **kwargs):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.03)
            return original_hash(path, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(scanner_module, "sha256_file", observed_hash)
    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="parallel", source_root=source)
        result = InventoryScanner(
            store,
            hash_workers=4,
            hash_queue_size=8,
        ).scan(project.id)

        assert result.complete
        assert maximum_active >= 2
        summary = store.get_job_run(result.run_id).summary
        assert summary["hashes_computed"] == 8
        assert summary["hashes_reused"] == 0
    finally:
        store.close()


def test_incremental_scan_checks_cached_files_in_parallel(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(12):
        (source / f"{index:02}.txt").write_bytes(f"payload-{index}".encode())

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="parallel-cache", source_root=source)
        assert InventoryScanner(store, hash_workers=4).scan(project.id).complete

        original_verify = scanner_module.verify_cached_digest
        lock = threading.Lock()
        active = 0
        maximum_active = 0

        def observed_verify(path, digest, **kwargs):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.02)
                return original_verify(path, digest, **kwargs)
            finally:
                with lock:
                    active -= 1

        monkeypatch.setattr(scanner_module, "verify_cached_digest", observed_verify)
        second = InventoryScanner(
            store,
            hash_workers=4,
            hash_queue_size=8,
        ).scan(project.id)

        assert second.complete
        assert maximum_active >= 2
        summary = store.get_job_run(second.run_id).summary
        assert summary["hashes_reused"] == 12
        assert summary["hashes_computed"] == 0
    finally:
        store.close()
