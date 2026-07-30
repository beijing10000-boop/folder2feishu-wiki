from __future__ import annotations

import pytest
from pydantic import ValidationError

from folder2feishu.api_models import ProjectCreate, SettingsUpdate


def test_project_requires_nonempty_source() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(name="Pilot", source_root="   ", target_wiki_url="https://x/wiki/abc")


def test_settings_enforce_safe_rate_limits() -> None:
    with pytest.raises(ValidationError):
        SettingsUpdate(
            app_id="cli_test",
            app_secret="secret",
            upload_qps=5,
        )
