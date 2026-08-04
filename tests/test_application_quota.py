from __future__ import annotations

from pathlib import Path

import pytest

from folder2feishu.application import ApplicationServices, QuotaCapacityUnknown
from folder2feishu.core import RemoteStatus
from folder2feishu.core import scanner as scanner_module
from folder2feishu.feishu import FeishuError
from folder2feishu.runtime import RuntimePaths
from folder2feishu.security import MemoryCredentialStore


def _quota(
    *,
    used: str = "100",
    total: str = "1000",
    unlimited: bool = False,
    tenant_exceeded: bool = False,
    list_name: str = "biz_infos",
) -> dict:
    return {
        list_name: [
            {
                "name": "ccm",
                "used": used,
                "quota": total,
                "unlimited": unlimited,
            }
        ],
        "is_tenant_quota_exceeded": tenant_exceeded,
    }


def test_quota_capacity_compares_inventory_bytes_with_available_ccm_bytes() -> None:
    ok, message = ApplicationServices._quota_capacity_check(
        _quota(),
        required_bytes=901,
    )

    assert ok is False
    assert "可用 900 字节" in message
    assert "本地待迁移 901 字节" in message

    ok, message = ApplicationServices._quota_capacity_check(
        _quota(),
        required_bytes=900,
    )

    assert ok is True
    assert "容量充足" in message


def test_quota_capacity_accepts_unlimited_and_compatibility_list_name() -> None:
    ok, message = ApplicationServices._quota_capacity_check(
        _quota(unlimited=True, list_name="biz_lists"),
        required_bytes=10_000_000,
    )

    assert ok is True
    assert "不限额" in message


def test_preflight_warns_but_does_not_block_onedrive_placeholders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "later.xlsx").write_bytes(b"placeholder-content")
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    project = services.create_project(
        name="placeholder",
        source_root=str(source),
        target_wiki_url="https://example.feishu.cn/drive/folder/folder-token",
    )
    calls = 0

    def placeholder_on_file(_: int) -> tuple[bool, bool, bool]:
        nonlocal calls
        calls += 1
        return (False, False, False) if calls == 1 else (True, False, False)

    monkeypatch.setattr(scanner_module, "file_attribute_flags", placeholder_on_file)
    try:
        services.scanner.scan(project.id)
        report = services.preflight(project.id)
    finally:
        services.close()

    source_check = next(check for check in report.checks if check["code"] == "source_items")
    assert source_check["status"] == "warning"
    assert source_check["blocking"] is False
    assert "1 个占位对象" in source_check["message"]
    assert "不阻断其他文件" in source_check["message"]


@pytest.mark.parametrize("unlimited", ["true", " TRUE ", 1])
def test_quota_capacity_accepts_compatible_true_values(unlimited: object) -> None:
    payload = _quota()
    payload["biz_infos"][0]["unlimited"] = unlimited

    ok, message = ApplicationServices._quota_capacity_check(
        payload,
        required_bytes=10_000_000,
    )

    assert ok is True
    assert "不限额" in message


@pytest.mark.parametrize("unlimited", ["false", " 0 ", 0])
def test_quota_capacity_accepts_compatible_false_values(unlimited: object) -> None:
    payload = _quota()
    payload["biz_infos"][0]["unlimited"] = unlimited

    ok, message = ApplicationServices._quota_capacity_check(
        payload,
        required_bytes=1,
    )

    assert ok is True
    assert "容量充足" in message


def test_quota_capacity_uses_numeric_quota_when_unlimited_is_unknown() -> None:
    payload = _quota()
    payload["biz_infos"][0]["unlimited"] = None

    ok, message = ApplicationServices._quota_capacity_check(
        payload,
        required_bytes=900,
    )

    assert ok is True
    assert "容量充足" in message


def test_quota_capacity_is_noncomparable_without_unlimited_or_quota() -> None:
    payload = _quota()
    payload["biz_infos"][0]["unlimited"] = None
    payload["biz_infos"][0]["quota"] = None

    with pytest.raises(QuotaCapacityUnknown, match="不阻断迁移"):
        ApplicationServices._quota_capacity_check(payload, required_bytes=1)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"is_tenant_quota_exceeded": False}, "业务容量列表"),
        (
            {"biz_infos": [], "is_tenant_quota_exceeded": False},
            "云文档容量（ccm）",
        ),
        (
            {
                "biz_infos": [
                    {
                        "name": "ccm",
                        "used": "not-a-number",
                        "quota": "1000",
                        "unlimited": False,
                    }
                ],
                "is_tenant_quota_exceeded": False,
            },
            "used",
        ),
        (
            {
                "biz_infos": [
                    {
                        "name": "ccm",
                        "used": "100",
                        "quota": "invalid",
                        "unlimited": False,
                    }
                ],
                "is_tenant_quota_exceeded": False,
            },
            "quota",
        ),
        (
            {
                "biz_infos": [
                    {
                        "name": "ccm",
                        "used": "100",
                        "quota": "1000",
                        "unlimited": False,
                    }
                ]
            },
            "租户超额状态",
        ),
    ],
)
def test_quota_capacity_fails_closed_on_missing_or_malformed_fields(
    payload: dict,
    expected: str,
) -> None:
    with pytest.raises(FeishuError, match=expected):
        ApplicationServices._quota_capacity_check(payload, required_bytes=1)


def test_quota_capacity_blocks_when_tenant_is_already_over_quota() -> None:
    ok, message = ApplicationServices._quota_capacity_check(
        _quota(tenant_exceeded=True),
        required_bytes=1,
    )

    assert ok is False
    assert "租户容量已超限" in message


def test_quota_capacity_blocks_when_ccm_usage_exceeds_its_quota() -> None:
    ok, message = ApplicationServices._quota_capacity_check(
        _quota(used="1001", total="1000"),
        required_bytes=0,
    )

    assert ok is False
    assert "云文档容量已超限" in message


def test_preflight_is_blocked_when_inventory_exceeds_ccm_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.bin").write_bytes(b"x" * 901)
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    project = services.create_project(
        name="capacity-check",
        source_root=str(source),
        target_wiki_url="https://example.feishu.cn/drive/folder/ABCDEFGHIJKL",
    )
    services.scanner.scan(project.id)

    class FakeDrive:
        @staticmethod
        def list_folder(_: str) -> list[dict]:
            return []

        @staticmethod
        def get_current_user_info() -> dict:
            return {"user_id": "current-user"}

        @staticmethod
        def can_edit_wiki(_: str) -> bool:
            return True

        @staticmethod
        def get_root_folder_token() -> str:
            return "drive-root"

        @staticmethod
        def get_quota_detail(_: str) -> dict:
            return _quota()

    class FakeWiki:
        @staticmethod
        def get_node(_: str) -> dict:
            return {
                "space_id": "space",
                "node_token": "parent",
                "parent_node_token": "",
            }

        @staticmethod
        def list_children(_: str, __: str) -> list[dict]:
            return []

    monkeypatch.setattr(
        services,
        "auth_status",
        lambda: {
            "authorized": True,
            "message": "授权可用",
        },
    )
    monkeypatch.setattr(
        services,
        "feishu_services",
        lambda: (FakeDrive(), FakeWiki()),
    )
    try:
        report = services.preflight(project.id)
    finally:
        services.close()

    quota_check = next(check for check in report.checks if check["code"] == "drive_quota")
    assert report.ready is False
    assert quota_check["blocking"] is True
    assert quota_check["status"] == "error"
    assert "容量不足" in quota_check["message"]


def test_preflight_warns_but_remains_ready_when_quota_limit_is_not_returned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.bin").write_bytes(b"x")
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    project = services.create_project(
        name="unknown-capacity-check",
        source_root=str(source),
        target_wiki_url="https://example.feishu.cn/drive/folder/ABCDEFGHIJKL",
    )
    services.scanner.scan(project.id)

    class FakeDrive:
        @staticmethod
        def list_folder(_: str) -> list[dict]:
            return []

        @staticmethod
        def get_current_user_info() -> dict:
            return {"user_id": "current-user"}

        @staticmethod
        def can_edit_wiki(_: str) -> bool:
            return True

        @staticmethod
        def get_root_folder_token() -> str:
            return "drive-root"

        @staticmethod
        def get_quota_detail(_: str) -> dict:
            payload = _quota()
            payload["biz_infos"][0]["quota"] = None
            payload["biz_infos"][0]["unlimited"] = None
            return payload

    class FakeWiki:
        @staticmethod
        def get_node(_: str) -> dict:
            return {
                "space_id": "space",
                "node_token": "parent",
                "parent_node_token": "",
            }

        @staticmethod
        def list_children(_: str, __: str) -> list[dict]:
            return []

    monkeypatch.setattr(
        services,
        "auth_status",
        lambda: {
            "authorized": True,
            "message": "授权可用",
        },
    )
    monkeypatch.setattr(
        services,
        "feishu_services",
        lambda: (FakeDrive(), FakeWiki()),
    )
    try:
        report = services.preflight(project.id)
    finally:
        services.close()

    quota_check = next(check for check in report.checks if check["code"] == "drive_quota")
    assert report.ready is True
    assert quota_check["blocking"] is False
    assert quota_check["status"] == "warning"
    assert "不阻断迁移" in quota_check["message"]


def test_preflight_uses_zero_pending_bytes_for_unchanged_second_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.bin").write_bytes(b"x" * 901)
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=MemoryCredentialStore(),
    )
    project = services.create_project(
        name="incremental-capacity-check",
        source_root=str(source),
        target_wiki_url="https://example.feishu.cn/drive/folder/ABCDEFGHIJKL",
    )
    services.scanner.scan(project.id)
    item = next(
        item
        for item in services.store.list_inventory(project.id, present=True)
        if item.kind.value == "FILE"
    )
    services.store.upsert_remote_mapping(
        project_id=project.id,
        inventory_item_id=item.id,
        item_kind=item.kind,
        last_source_rel_path=item.rel_path,
        source_file_identity=item.file_identity,
        source_sha256=item.sha256,
        source_size=item.size,
        wiki_space_id="space",
        wiki_node_token="wiki-file",
        object_token="file-token",
        remote_parent_node_token="parent",
        remote_title=item.name,
        remote_status=RemoteStatus.ACTIVE,
    )
    services.scanner.scan(project.id)

    class FakeDrive:
        @staticmethod
        def list_folder(_: str) -> list[dict]:
            return []

        @staticmethod
        def get_current_user_info() -> dict:
            return {"user_id": "current-user"}

        @staticmethod
        def can_edit_wiki(_: str) -> bool:
            return True

        @staticmethod
        def get_root_folder_token() -> str:
            return "drive-root"

        @staticmethod
        def get_quota_detail(_: str) -> dict:
            # No spare capacity remains, but the unchanged incremental run
            # requires no new upload and must not be falsely blocked.
            return _quota(used="1000", total="1000")

    class FakeWiki:
        @staticmethod
        def get_node(_: str) -> dict:
            return {
                "space_id": "space",
                "node_token": "parent",
                "parent_node_token": "",
            }

        @staticmethod
        def list_children(_: str, __: str) -> list[dict]:
            return []

    monkeypatch.setattr(
        services,
        "auth_status",
        lambda: {
            "authorized": True,
            "message": "授权可用",
        },
    )
    monkeypatch.setattr(
        services,
        "feishu_services",
        lambda: (FakeDrive(), FakeWiki()),
    )
    try:
        report = services.preflight(project.id)
    finally:
        services.close()

    quota_check = next(check for check in report.checks if check["code"] == "drive_quota")
    assert report.ready is True
    assert quota_check["blocking"] is False
    assert quota_check["status"] == "ok"
    assert "本地待迁移 0 字节" in quota_check["message"]
