from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from folder2feishu.feishu import (
    DriveService,
    FeishuAPIClient,
    FeishuError,
    OAuthClient,
    StoredUserTokenProvider,
    TokenBundle,
    WikiService,
)
from folder2feishu.runtime import RuntimePaths
from folder2feishu.security import MemoryCredentialStore
from folder2feishu.settings import DEFAULT_SCOPES
from folder2feishu.verification import (
    verify_app_credentials,
    verify_oauth_identity,
    verify_source_root,
    verify_wiki_target,
)


def _provider(
    *,
    scopes: set[str] | None = None,
) -> StoredUserTokenProvider:
    credentials = MemoryCredentialStore()
    credentials.set(
        "feishu.user_token_bundle",
        TokenBundle(
            access_token="user-access-token",
            refresh_token="refresh-token",
            expires_at=9_999_999_000,
            refresh_expires_at=9_999_999_999,
            scopes=frozenset(scopes or DEFAULT_SCOPES),
        ).to_json(),
    )
    oauth = OAuthClient(
        client_id="cli_verify",
        client_secret_provider=lambda: "app-secret",
        required_scopes=DEFAULT_SCOPES,
    )
    return StoredUserTokenProvider(oauth, credentials, now=lambda: 1_000)


def _api(handler) -> tuple[FeishuAPIClient, httpx.Client]:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        FeishuAPIClient(
            lambda: "user-access-token",
            client=http,
            sleeper=lambda _: None,
        ),
        http,
    )


def test_app_credential_verification_discards_tenant_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/open-apis/auth/v3/tenant_access_token/internal/"
        assert json.loads(request.content) == {
            "app_id": "cli_verify",
            "app_secret": "top-secret",
        }
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "tenant_access_token": "tenant-token-must-not-escape",
                "expire": 7200,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify_app_credentials(
            "cli_verify",
            "top-secret",
            client=client,
        )

    serialized = json.dumps(result.as_dict(), ensure_ascii=False)
    assert result.ok is True
    assert result.details == {"credential_valid": True}
    assert "tenant-token-must-not-escape" not in serialized
    assert len(requests) == 1


def test_invalid_app_credentials_return_a_generic_error_without_secrets() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": 10003,
                "msg": "invalid app secret top-secret-reflected",
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(FeishuError) as captured,
    ):
        verify_app_credentials(
            "cli_invalid",
            "top-secret-reflected",
            client=client,
        )

    message = str(captured.value)
    assert "飞书未接受当前 App ID / App Secret" in message
    assert "10003" in message
    assert "top-secret-reflected" not in message


def test_oauth_verification_requires_all_scopes_before_user_info() -> None:
    provider = _provider(scopes=set(DEFAULT_SCOPES) - {"wiki:wiki"})
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    api, http = _api(handler)
    try:
        with pytest.raises(Exception, match="wiki:wiki"):
            verify_oauth_identity(provider, DriveService(api))
    finally:
        api.close()
        http.close()
    assert called is False


def test_oauth_and_wiki_target_use_real_read_only_openapi_calls() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer user-access-token"
        if request.url.path == "/open-apis/authen/v1/user_info":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "user_id": "ou_internal_id_not_for_browser",
                        "name": "迁移管理员",
                    },
                },
            )
        if request.url.path == "/open-apis/wiki/v2/spaces/get_node":
            assert request.url.params["token"] == "WikiParentToken99"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "node": {
                            "space_id": "space-100",
                            "node_token": "WikiParentToken99",
                            "title": "FabDazzle",
                        }
                    },
                },
            )
        if request.url.path == "/open-apis/drive/v1/permissions/WikiParentToken99/members/auth":
            assert request.url.params["type"] == "wiki"
            assert request.url.params["action"] == "edit"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"auth_result": True}},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    provider = _provider()
    api, http = _api(handler)
    drive = DriveService(api)
    wiki = WikiService(api)
    try:
        oauth_result = verify_oauth_identity(provider, drive)
        target_result = verify_wiki_target(
            "https://example.feishu.cn/wiki/WikiParentToken99?fromScene=spaceOverview",
            token_provider=provider,
            drive=drive,
            wiki=wiki,
        )
    finally:
        api.close()
        http.close()

    assert oauth_result.details == {"user_name": "迁移管理员", "scope_count": 6}
    assert "ou_internal_id_not_for_browser" not in json.dumps(
        oauth_result.as_dict(),
        ensure_ascii=False,
    )
    assert target_result.details["space_id"] == "space-100"
    assert target_result.details["node_token"] == "WikiParentToken99"
    assert target_result.details["container_edit_requires_pilot"] is True
    assert not any("nodes/create" in path for path in paths)


def test_source_verification_is_shallow_and_rejects_runtime_inside_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "OneDrive"
    source.mkdir()
    (source / "visible.txt").write_text("visible", encoding="utf-8")

    safe_runtime = RuntimePaths.discover(tmp_path / "runtime").ensure()
    before = {
        path.relative_to(source): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
    }
    result = verify_source_root(str(source), safe_runtime)
    after = {
        path.relative_to(source): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.rglob("*")
    }
    assert result.ok is True
    assert result.details["normalized_path"] == str(source.resolve())
    assert after == before

    unsafe_runtime = RuntimePaths.discover(source / ".folder2feishu").ensure()
    with pytest.raises(ValueError, match="应用数据目录不能位于迁移源目录"):
        verify_source_root(str(source), unsafe_runtime)
