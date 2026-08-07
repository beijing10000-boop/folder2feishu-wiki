from __future__ import annotations

from pathlib import Path

import pytest

from folder2feishu.application import ApplicationServices
from folder2feishu.core import RunStatus, RunType
from folder2feishu.runtime import RuntimePaths
from folder2feishu.security import MemoryCredentialStore
from folder2feishu.settings import PublicSettings


class RecordingClient:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def configured_services(tmp_path: Path) -> tuple[ApplicationServices, PublicSettings]:
    credentials = MemoryCredentialStore()
    credentials.set("feishu.app_secret", "secret")
    services = ApplicationServices(
        paths=RuntimePaths.discover(tmp_path / "runtime"),
        credentials=credentials,
    )
    settings = PublicSettings(app_id="cli_test")
    services.settings_store.save(settings)
    return services, settings


def test_identical_settings_save_keeps_active_client_open(tmp_path: Path) -> None:
    services, settings = configured_services(tmp_path)
    client = RecordingClient()
    services._client = client  # type: ignore[assignment]

    try:
        payload = services.save_settings(settings)

        assert payload["app_id"] == "cli_test"
        assert services._client is client
        assert client.close_calls == 0
        assert services._retired_clients == []
    finally:
        services.close()

    assert client.close_calls == 1


def test_active_migration_rejects_connection_setting_changes(tmp_path: Path) -> None:
    services, settings = configured_services(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    project = services.store.create_project(name="active", source_root=source)
    services.store.create_job_run(
        project.id,
        RunType.MIGRATION,
        status=RunStatus.RUNNING,
    )
    client = RecordingClient()
    services._client = client  # type: ignore[assignment]
    changed = PublicSettings(app_id=settings.app_id, upload_qps=4.0)

    try:
        with pytest.raises(ValueError, match="连接配置已锁定"):
            services.save_settings(changed)

        assert services.settings_store.load().upload_qps == 5.0
        assert services._client is client
        assert client.close_calls == 0
    finally:
        services.close()


def test_changed_settings_retire_client_until_shutdown(tmp_path: Path) -> None:
    services, settings = configured_services(tmp_path)
    client = RecordingClient()
    services._client = client  # type: ignore[assignment]
    changed = PublicSettings(app_id=settings.app_id, upload_qps=4.0)

    services.save_settings(changed)

    assert services._client is None
    assert services._retired_clients == [client]
    assert client.close_calls == 0

    services.close()

    assert client.close_calls == 1
