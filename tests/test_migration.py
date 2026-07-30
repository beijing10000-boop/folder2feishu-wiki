import time
from pathlib import Path

from app.db import Database
from app.migrator import MigrationManager, SourceScanner


class FakeFeishu:
    def __init__(self):
        self.nodes = {
            "targettoken123": {
                "space_id": "space-1",
                "node_token": "targettoken123",
                "title": "迁移入口",
            }
        }
        self.children = {"targettoken123": []}
        self.created = []
        self.uploaded = []
        self.mounted = []
        self.serial = 0

    def get_node(self, token):
        return self.nodes[token]

    def list_children(self, _space_id, parent):
        return list(self.children.get(parent, []))

    def create_folder_node(self, _space_id, title, parent):
        self.serial += 1
        token = f"node-{self.serial}"
        node = {"node_token": token, "obj_token": f"doc-{self.serial}", "title": title}
        self.nodes[token] = {"space_id": "space-1", **node}
        self.children.setdefault(parent, []).append(node)
        self.children.setdefault(token, [])
        self.created.append((parent, title, token))
        return node

    def upload_file(self, local_path, file_name):
        token = f"file-{len(self.uploaded) + 1}"
        self.uploaded.append((Path(local_path).name, file_name, token))
        return token

    def mount_file(self, _space_id, file_token, parent):
        token = f"wiki-{len(self.mounted) + 1}"
        self.mounted.append((file_token, parent, token))
        return token


def wait_for_job(manager, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = manager.status()
        if status["state"] in {"completed", "failed", "stopped"}:
            return status
        time.sleep(0.02)
    raise AssertionError(f"migration job did not finish: {manager.status()}")


def test_scan_and_migrate_preserves_original_tree(tmp_path):
    source = tmp_path / "Team FabDazzle - 文档"
    (source / "Apparel" / "Reports").mkdir(parents=True)
    (source / "Apparel" / "brief.docx").write_bytes(b"brief")
    (source / "Apparel" / "Reports" / "weekly.xlsx").write_bytes(b"weekly")
    (source / "root.pdf").write_bytes(b"pdf")
    (source / "empty.txt").write_bytes(b"")

    db = Database(tmp_path / "data" / "migration.db")
    result = SourceScanner(db).scan(str(source))
    assert result["folders"] == 2
    assert result["files"] == 4

    fake = FakeFeishu()
    manager = MigrationManager(db, lambda: fake)
    target = manager.configure_target("targettoken123", create_wrapper=True)
    assert target["space_id"] == "space-1"

    manager.start()
    status = wait_for_job(manager)
    assert status["state"] == "completed"

    summary = db.summary()
    assert summary["folders"]["success"] == 2
    assert summary["files"]["success"]["count"] == 3
    assert summary["files"]["failed"]["count"] == 1

    created_by_title = {title: (parent, token) for parent, title, token in fake.created}
    wrapper_token = created_by_title[source.name][1]
    apparel_parent, apparel_token = created_by_title["Apparel"]
    reports_parent, _reports_token = created_by_title["Reports"]
    assert apparel_parent == wrapper_token
    assert reports_parent == apparel_token

    assert len(fake.uploaded) == 3
    assert len(fake.mounted) == 3

    deadline = time.time() + 2
    while manager.status()["alive"] and time.time() < deadline:
        time.sleep(0.01)
    manager.start()
    wait_for_job(manager)
    assert len(fake.uploaded) == 3
    assert len(fake.mounted) == 3


def test_rescan_marks_successful_changed_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    item = source / "one.txt"
    item.write_text("one", encoding="utf-8")
    db = Database(tmp_path / "migration.db")
    scanner = SourceScanner(db)
    scanner.scan(str(source))
    db.update_row("files", "one.txt", status="success", wiki_token="wiki-1")

    time.sleep(0.01)
    item.write_text("changed", encoding="utf-8")
    scanner.scan(str(source))

    changed = db.rows("files", ("changed",))
    assert len(changed) == 1
    assert "未自动覆盖" in changed[0]["error"]
