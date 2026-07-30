from __future__ import annotations

import json
import os

import pytest

from folder2feishu.security import (
    DPAPICredentialStore,
    DPAPIUnavailableError,
    MemoryCredentialStore,
    RestrictedFileCredentialStore,
    create_credential_store,
)


def test_memory_store_is_explicitly_non_persistent():
    store = MemoryCredentialStore()
    store.set("feishu.token", "secret")
    assert store.get("feishu.token") == "secret"
    assert store.backend_name == "memory-test-fallback"
    assert store.persistent is False
    store.delete("feishu.token")
    assert store.get("feishu.token") is None


def test_restricted_file_fallback_is_visibly_marked(tmp_path):
    path = tmp_path / "test-credentials.json"
    store = RestrictedFileCredentialStore(path)
    store.set("feishu.token", "test-only-secret")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["backend"] == "restricted-file-test-fallback-plaintext"
    assert "TEST-ONLY FALLBACK" in payload["warning"]
    assert store.get("feishu.token") == "test-only-secret"
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_platform_factory_never_silently_uses_plaintext(tmp_path):
    path = tmp_path / "credentials.json"
    if os.name == "nt":
        assert isinstance(create_credential_store(path), DPAPICredentialStore)
    else:
        with pytest.raises(DPAPIUnavailableError):
            create_credential_store(path)
        assert isinstance(
            create_credential_store(path, allow_test_fallback=True),
            RestrictedFileCredentialStore,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration")
def test_dpapi_round_trip_does_not_store_plaintext(tmp_path):
    path = tmp_path / "credentials.dpapi.json"
    store = DPAPICredentialStore(path)
    store.set("feishu.app_secret", "must-not-be-plaintext")
    assert store.get("feishu.app_secret") == "must-not-be-plaintext"
    assert "must-not-be-plaintext" not in path.read_text(encoding="utf-8")
