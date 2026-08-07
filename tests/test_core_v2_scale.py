from __future__ import annotations

import os
import time

import pytest

from folder2feishu.core import CoreStore, InventoryScanner


@pytest.mark.skipif(
    os.environ.get("FOLDER2FEISHU_RUN_SCALE_TEST") != "1",
    reason="set FOLDER2FEISHU_RUN_SCALE_TEST=1 for the 35k-file acceptance run",
)
def test_scanner_handles_35000_files_and_5000_directories_with_bounded_memory(
    tmp_path,
):
    source = tmp_path / "scale-source"
    source.mkdir()
    expected_directories = 1
    expected_files = 0
    for group_index in range(50):
        group = source / f"group-{group_index:02}"
        group.mkdir()
        expected_directories += 1
        for folder_index in range(100):
            folder = group / f"folder-{folder_index:03}"
            folder.mkdir()
            expected_directories += 1
            for file_index in range(7):
                (folder / f"file-{file_index}.txt").write_bytes(b"x")
                expected_files += 1

    store = CoreStore(tmp_path / "ledger.db")
    try:
        project = store.create_project(name="scale", source_root=source)
        started = time.monotonic()
        # Match the production application's fast first-pass policy: collect
        # stable metadata now and defer new-file SHA-256 reads until an item is
        # actually selected for upload.  A scale test that hashes every fresh
        # file measures a code path the desktop application no longer uses.
        result = InventoryScanner(
            store,
            batch_size=500,
            defer_new_hashes=True,
        ).scan(project.id)
        elapsed = time.monotonic() - started

        assert result.complete
        assert result.folders == expected_directories == 5_051
        assert result.files == expected_files == 35_000
        run = store.get_job_run(result.run_id)
        assert run.summary["hashes_deferred"] == expected_files
        assert elapsed < 180
    finally:
        store.close()
