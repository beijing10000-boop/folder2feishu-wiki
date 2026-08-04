from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from folder2feishu.feishu import (
    DEFAULT_SCOPES,
    MissingScopesError,
    OAuthClient,
    OAuthStateError,
    StoredUserTokenProvider,
    TokenBundle,
    pkce_challenge,
)
from folder2feishu.security import MemoryCredentialStore


def _token_payload(**overrides):
    payload = {
        "code": 0,
        "access_token": "u-access-1",
        "expires_in": 7200,
        "refresh_token": "refresh-1",
        "refresh_token_expires_in": 604800,
        "scope": " ".join(sorted(DEFAULT_SCOPES)),
        "token_type": "Bearer",
    }
    payload.update(overrides)
    return payload


def test_oauth_v2_uses_pkce_state_and_json_token_exchange():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_token_payload())

    oauth = OAuthClient(
        client_id="cli_test",
        client_secret_provider=lambda: "app-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: 1000,
    )
    authorization = oauth.begin_authorization("http://127.0.0.1:8000/oauth/callback")
    query = parse_qs(urlparse(authorization.authorization_url).query)
    assert query["state"] == [authorization.state]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [pkce_challenge(authorization.code_verifier)]
    assert set(query["scope"][0].split()) == set(DEFAULT_SCOPES)

    bundle = oauth.complete_authorization(code="authorization-code", state=authorization.state)
    request = seen["request"]
    assert request.url.path == "/open-apis/authen/v2/oauth/token"
    assert request.headers["content-type"].startswith("application/json")
    assert seen["payload"] == {
        "grant_type": "authorization_code",
        "client_id": "cli_test",
        "client_secret": "app-secret",
        "code": "authorization-code",
        "redirect_uri": "http://127.0.0.1:8000/oauth/callback",
        "code_verifier": authorization.code_verifier,
    }
    assert bundle.access_token == "u-access-1"

    with pytest.raises(OAuthStateError):
        oauth.complete_authorization(code="replay", state=authorization.state)


def test_refresh_rotates_single_use_token_and_persists_before_return():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_token_payload(
                access_token="u-access-2",
                refresh_token="refresh-2",
            ),
        )

    store = MemoryCredentialStore()
    oauth = OAuthClient(
        client_id="cli_test",
        client_secret_provider=lambda: "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        now=lambda: 2000,
    )
    provider = StoredUserTokenProvider(oauth, store, now=lambda: 2000)
    expired = TokenBundle(
        access_token="u-expired",
        refresh_token="refresh-1",
        expires_at=1000,
        refresh_expires_at=9999,
        scopes=DEFAULT_SCOPES,
    )
    store.set(provider.credential_key, expired.to_json())

    assert provider() == "u-access-2"
    assert captured["json"] == {
        "grant_type": "refresh_token",
        "client_id": "cli_test",
        "client_secret": "secret",
        "refresh_token": "refresh-1",
    }
    persisted = provider.load()
    assert persisted is not None
    assert persisted.refresh_token == "refresh-2"


def test_missing_required_scope_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json=_token_payload(scope="offline_access drive:drive"),
        )

    oauth = OAuthClient(
        client_id="cli_test",
        client_secret_provider=lambda: "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    authorization = oauth.begin_authorization("http://127.0.0.1:8000/callback")
    with pytest.raises(MissingScopesError) as caught:
        oauth.complete_authorization(code="code", state=authorization.state)
    assert "drive:file:upload" in caught.value.missing_scopes
