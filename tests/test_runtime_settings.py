from __future__ import annotations

import json
from pathlib import Path

import pytest

from folder2feishu.runtime import RuntimePaths, assert_runtime_outside_source
from folder2feishu.settings import DEFAULT_SCOPES, PublicSettings, SettingsStore


def test_runtime_paths_are_created_outside_source(tmp_path: Path) -> None:
    paths = RuntimePaths.discover(tmp_path / "state").ensure()
    assert paths.database.parent == paths.base
    assert paths.logs.is_dir()
    assert paths.exports.is_dir()
    assert_runtime_outside_source(paths, tmp_path / "OneDrive")


def test_runtime_inside_source_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "OneDrive"
    paths = RuntimePaths.discover(source / ".state").ensure()
    with pytest.raises(ValueError, match="不能位于"):
        assert_runtime_outside_source(paths, source)


def test_settings_roundtrip_never_contains_secret(tmp_path: Path) -> None:
    assert "drive:file:upload" in DEFAULT_SCOPES
    paths = RuntimePaths.discover(tmp_path / "state")
    store = SettingsStore(paths)
    settings = PublicSettings(app_id="cli_example")
    store.save(settings)
    loaded = store.load()
    assert loaded.app_id == "cli_example"
    assert set(DEFAULT_SCOPES).issubset(loaded.scopes)
    on_disk = json.loads(paths.settings.read_text(encoding="utf-8"))
    assert "secret" not in " ".join(on_disk).lower()


def test_legacy_daily_budget_is_migrated_to_unlimited(tmp_path: Path) -> None:
    paths = RuntimePaths.discover(tmp_path / "state").ensure()
    paths.settings.write_text(
        json.dumps({"app_id": "cli_example", "daily_upload_budget": 9_500}),
        encoding="utf-8",
    )
    loaded = SettingsStore(paths).load()
    assert loaded.daily_upload_budget == 0


def test_legacy_default_wiki_rate_is_upgraded_but_custom_rate_is_preserved(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.discover(tmp_path / "state").ensure()
    paths.settings.write_text(
        json.dumps({"app_id": "cli_example", "wiki_calls_per_minute": 90}),
        encoding="utf-8",
    )
    assert SettingsStore(paths).load().wiki_calls_per_minute == 100

    paths.settings.write_text(
        json.dumps({"app_id": "cli_example", "wiki_calls_per_minute": 70}),
        encoding="utf-8",
    )
    assert SettingsStore(paths).load().wiki_calls_per_minute == 70


def test_settings_reject_external_bind_and_missing_scope() -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        PublicSettings(host="0.0.0.0").validate()
    with pytest.raises(ValueError, match="缺少"):
        PublicSettings(scopes=["wiki:wiki"]).validate()
