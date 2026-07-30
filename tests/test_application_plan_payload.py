from __future__ import annotations

from pathlib import Path

from folder2feishu.application import PLAN_PREVIEW_PER_KIND, ApplicationServices
from folder2feishu.core import InventoryState, ItemKind
from folder2feishu.runtime import RuntimePaths
from folder2feishu.security import MemoryCredentialStore


def test_large_plan_returns_bounded_samples_and_confirms_in_bulk(tmp_path: Path) -> None:
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    project = services.store.create_project(
        name="bounded-plan-preview",
        source_root=tmp_path / "source",
        target_wiki_url="https://example.feishu.cn/wiki/ABCDEFGHIJKL",
    )
    scan_id = "scan-large-plan"
    rows = [
        {
            "kind": ItemKind.FOLDER,
            "rel_path": "",
            "parent_rel_path": None,
            "name": "source",
            "depth": 0,
            "file_identity": "folder-root",
            "size": 0,
            "mtime_ns": 1,
            "sha256": None,
            "state": InventoryState.DISCOVERED,
        },
        *[
            {
                "kind": ItemKind.FILE,
                "rel_path": f"file-{index:04}.txt",
                "parent_rel_path": "",
                "name": f"file-{index:04}.txt",
                "depth": 1,
                "file_identity": f"file-{index:04}",
                "size": 1,
                "mtime_ns": index + 1,
                "sha256": f"{index:064x}",
                "state": InventoryState.DISCOVERED,
            }
            for index in range(PLAN_PREVIEW_PER_KIND + 50)
        ],
    ]
    try:
        services.store.replace_inventory(project.id, scan_id, rows)
        services.store.update_project(
            project.id,
            current_scan_id=scan_id,
            scan_complete=True,
        )
        result = services.planner.build(project.id)
        payload = services.plan_payload(project.id)

        assert result.estimated_upload_calls == PLAN_PREVIEW_PER_KIND + 50
        assert payload["total_actions"] == PLAN_PREVIEW_PER_KIND + 51
        assert payload["counts"] == {
            "CREATE_FOLDER": 1,
            "UPLOAD": PLAN_PREVIEW_PER_KIND + 50,
        }
        assert len(payload["actions"]) == PLAN_PREVIEW_PER_KIND + 1
        assert payload["preview_limit_per_kind"] == PLAN_PREVIEW_PER_KIND

        confirmed = services.confirm_latest_plan(project.id)
        assert confirmed["confirmed"] is True
        assert all(
            action.details.get("plan_confirmed")
            for action in services.store.list_plan_actions(project.id, plan_id=result.plan_id)
        )
    finally:
        services.close()
