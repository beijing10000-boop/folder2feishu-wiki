from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from folder2feishu.api import create_app
from folder2feishu.workspaces import WorkspaceManager, validate_workspace_name


def test_workspace_manager_creates_and_switches_isolated_runtime_folders(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    (root / "Team-FabDazzle").mkdir(parents=True)
    manager = WorkspaceManager(root)
    try:
        assert manager.active_name == "Team-FabDazzle"
        assert manager.active_services.paths.database.parent == root / "Team-FabDazzle"

        created = manager.create_workspace("JustFab")
        assert created["active_folder_name"] == "JustFab"
        assert (root / "JustFab" / "ledger.sqlite3").is_file()
        assert manager.active_services.paths.database.parent == root / "JustFab"

        selected = manager.select_workspace("Team-FabDazzle")
        assert selected["active_folder_name"] == "Team-FabDazzle"
        assert manager.active_services.paths.database.parent == root / "Team-FabDazzle"
    finally:
        manager.close()


def test_workspace_switch_is_blocked_while_current_runtime_has_active_work(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    (root / "A").mkdir(parents=True)
    (root / "B").mkdir()
    manager = WorkspaceManager(root, initial_runtime=root / "A")
    try:
        manager.active_services.store.job_status_counts = lambda: {"RUNNING": 1}  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="仍有运行、排队或暂停中的任务"):
            manager.select_workspace("B")
        assert manager.active_name == "A"
    finally:
        manager.close()


@pytest.mark.parametrize(
    "name",
    ["", "..", "a/b", "a\\b", "CON", "name.", ".hidden", "bad*name"],
)
def test_workspace_names_reject_unsafe_windows_paths(name: str) -> None:
    with pytest.raises(ValueError):
        validate_workspace_name(name)


def test_workspace_api_lists_creates_and_selects_data_projects(tmp_path: Path) -> None:
    root = tmp_path / "Projects"
    (root / "Existing").mkdir(parents=True)
    manager = WorkspaceManager(root)
    app = create_app(manager, static_root=Path("frontend/dist").resolve())
    try:
        with TestClient(app) as client:
            csrf = client.get("/api/v2/session").json()["csrf_token"]
            headers = {"X-F2F-CSRF": csrf}

            listed = client.get("/api/v2/workspaces").json()
            assert listed["active_folder_name"] == "Existing"
            assert [item["folder_name"] for item in listed["items"]] == ["Existing"]

            created = client.post(
                "/api/v2/workspaces",
                json={"name": "JustFab"},
                headers=headers,
            )
            assert created.status_code == 200
            assert created.json()["active_folder_name"] == "JustFab"

            selected = client.post(
                "/api/v2/workspaces/select",
                json={"name": "Existing"},
                headers=headers,
            )
            assert selected.status_code == 200
            assert selected.json()["active_folder_name"] == "Existing"
    finally:
        manager.close()
