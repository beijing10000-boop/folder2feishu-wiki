from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from folder2feishu.security import CredentialStore

from .errors import MissingScopesError, OAuthError, OAuthStateError

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
DEFAULT_SCOPES = frozenset(
    {
        "offline_access",
        "drive:drive",
        "drive:file:upload",
        "wiki:wiki",
        "drive:quota_detail:read_one",
        "contact:user.employee_id:readonly",
    }
)
APP_SECRET_CREDENTIAL = "feishu.app_secret"
USER_TOKEN_CREDENTIAL = "feishu.user_token_bundle"


def _normalize_scopes(scopes: Iterable[str]) -> frozenset[str]:
    normalized = frozenset(scope.strip() for scope in scopes if scope.strip())
    if not normalized:
        raise ValueError("at least one OAuth scope is required")
    if len(normalized) > 50:
        raise ValueError("Feishu authorization supports at most 50 scopes")
    return normalized


def _pkce_verifier() -> str:
    # 64 random bytes encode to 86 RFC 7636 unreserved characters.
    return secrets.token_urlsafe(64)


def pkce_challenge(verifier: str) -> str:
    if not 43 <= len(verifier) <= 128:
        raise ValueError("PKCE verifier length must be between 43 and 128")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class OAuthAuthorization:
    authorization_url: str
    state: str
    code_verifier: str = field(repr=False)
    code_challenge: str
    redirect_uri: str
    scopes: frozenset[str]
    expires_at: float


@dataclass(frozen=True, slots=True)
class TokenBundle:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float = 0
    refresh_expires_at: float | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    token_type: str = "Bearer"

    @classmethod
    def from_payload(
        cls, payload: dict, *, now: float, previous_refresh_token: str | None = None
    ) -> TokenBundle:
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise OAuthError("Feishu token response is missing access_token")
        expires_in = int(payload.get("expires_in") or 0)
        if expires_in <= 0:
            raise OAuthError("Feishu token response has an invalid expires_in")
        refresh_token = str(payload.get("refresh_token") or "") or previous_refresh_token
        refresh_lifetime = int(
            payload.get("refresh_token_expires_in") or payload.get("refresh_expires_in") or 0
        )
        scope_value = payload.get("scope") or ""
        if isinstance(scope_value, str):
            scopes = frozenset(scope_value.split())
        elif isinstance(scope_value, list):
            scopes = frozenset(str(scope) for scope in scope_value)
        else:
            raise OAuthError("Feishu token response has an invalid scope")
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=now + expires_in,
            refresh_expires_at=(now + refresh_lifetime) if refresh_lifetime else None,
            scopes=scopes,
            token_type=str(payload.get("token_type") or "Bearer"),
        )

    def require_scopes(self, required_scopes: Iterable[str]) -> None:
        required = _normalize_scopes(required_scopes)
        missing = set(required - self.scopes)
        if missing:
            raise MissingScopesError(missing)

    def access_valid(self, *, now: float, skew_seconds: float = 300) -> bool:
        return bool(self.access_token and now < self.expires_at - skew_seconds)

    def refresh_valid(self, *, now: float, skew_seconds: float = 60) -> bool:
        if not self.refresh_token:
            return False
        return self.refresh_expires_at is None or now < self.refresh_expires_at - skew_seconds

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "refresh_expires_at": self.refresh_expires_at,
                "scopes": sorted(self.scopes),
                "token_type": self.token_type,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> TokenBundle:
        try:
            payload = json.loads(value)
            return cls(
                access_token=str(payload["access_token"]),
                refresh_token=(
                    str(payload["refresh_token"]) if payload.get("refresh_token") else None
                ),
                expires_at=float(payload["expires_at"]),
                refresh_expires_at=(
                    float(payload["refresh_expires_at"])
                    if payload.get("refresh_expires_at") is not None
                    else None
                ),
                scopes=frozenset(str(scope) for scope in payload.get("scopes", ())),
                token_type=str(payload.get("token_type") or "Bearer"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OAuthError("stored OAuth token bundle is malformed") from exc


class OAuthStateRegistry:
    """One-time in-memory OAuth state and PKCE verifier registry."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        default_ttl_seconds: float = 600,
    ) -> None:
        self._now = now
        self._ttl = default_ttl_seconds
        self._pending: dict[str, OAuthAuthorization] = {}
        self._lock = threading.RLock()

    def issue(self, client_id: str, redirect_uri: str, scopes: Iterable[str]) -> OAuthAuthorization:
        if not client_id.strip():
            raise ValueError("client_id is required")
        if not redirect_uri.startswith(("http://", "https://")):
            raise ValueError("redirect_uri must be an absolute HTTP(S) URL")
        normalized_scopes = _normalize_scopes(scopes)
        state = secrets.token_urlsafe(32)
        verifier = _pkce_verifier()
        challenge = pkce_challenge(verifier)
        expires_at = self._now() + self._ttl
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(sorted(normalized_scopes)),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorization = OAuthAuthorization(
            authorization_url=f"{AUTHORIZE_URL}?{urlencode(params)}",
            state=state,
            code_verifier=verifier,
            code_challenge=challenge,
            redirect_uri=redirect_uri,
            scopes=normalized_scopes,
            expires_at=expires_at,
        )
        with self._lock:
            self._pending[state] = authorization
            self._remove_expired()
        return authorization

    def consume(self, state: str) -> OAuthAuthorization:
        if not state:
            raise OAuthStateError("OAuth state is missing")
        with self._lock:
            # Constant-time comparison avoids making state validity observable
            # through a custom registry wrapper.
            matched_key = next(
                (candidate for candidate in self._pending if hmac.compare_digest(candidate, state)),
                None,
            )
            authorization = self._pending.pop(matched_key) if matched_key is not None else None
        if authorization is None:
            raise OAuthStateError("OAuth state is invalid or has already been used")
        if self._now() >= authorization.expires_at:
            raise OAuthStateError("OAuth state has expired")
        return authorization

    def _remove_expired(self) -> None:
        now = self._now()
        expired = [
            state
            for state, authorization in self._pending.items()
            if now >= authorization.expires_at
        ]
        for state in expired:
            self._pending.pop(state, None)


class OAuthClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret_provider: Callable[[], str],
        client: httpx.Client | None = None,
        states: OAuthStateRegistry | None = None,
        required_scopes: Iterable[str] = DEFAULT_SCOPES,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not client_id.strip():
            raise ValueError("client_id is required")
        self.client_id = client_id
        self._client_secret_provider = client_secret_provider
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
        )
        self.states = states or OAuthStateRegistry(now=now)
        self.required_scopes = _normalize_scopes(required_scopes)
        self._now = now

    def begin_authorization(
        self,
        redirect_uri: str,
        *,
        scopes: Iterable[str] | None = None,
    ) -> OAuthAuthorization:
        requested = _normalize_scopes(scopes or self.required_scopes)
        missing = set(self.required_scopes - requested)
        if missing:
            raise MissingScopesError(missing)
        return self.states.issue(self.client_id, redirect_uri, requested)

    def complete_authorization(self, *, code: str, state: str) -> TokenBundle:
        if not code:
            raise OAuthError("authorization code is missing")
        authorization = self.states.consume(state)
        payload = self._post_token(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self._client_secret(),
                "code": code,
                "redirect_uri": authorization.redirect_uri,
                "code_verifier": authorization.code_verifier,
            }
        )
        bundle = TokenBundle.from_payload(payload, now=self._now())
        bundle.require_scopes(self.required_scopes)
        return bundle

    def refresh(self, bundle: TokenBundle) -> TokenBundle:
        now = self._now()
        if not bundle.refresh_valid(now=now):
            raise OAuthError("refresh_token is missing or expired; authorize again")
        payload = self._post_token(
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self._client_secret(),
                "refresh_token": bundle.refresh_token,
            }
        )
        refreshed = TokenBundle.from_payload(
            payload, now=now, previous_refresh_token=bundle.refresh_token
        )
        refreshed.require_scopes(self.required_scopes)
        if refreshed.refresh_token == bundle.refresh_token:
            # Feishu v2 refresh tokens are single-use and must rotate. Keeping
            # the old token would make the next unattended refresh fail.
            raise OAuthError("Feishu did not rotate the single-use refresh_token")
        return refreshed

    def _client_secret(self) -> str:
        secret = self._client_secret_provider()
        if not secret:
            raise OAuthError("Feishu app secret is not configured")
        return secret

    def _post_token(self, payload: dict[str, str | None]) -> dict:
        try:
            response = self.client.post(
                TOKEN_URL,
                json=payload,
                headers={"Accept": "application/json"},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise OAuthError("unable to reach the Feishu OAuth endpoint") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise OAuthError(
                f"Feishu OAuth returned non-JSON HTTP {response.status_code}",
                status_code=response.status_code,
            ) from exc
        if not isinstance(body, dict):
            raise OAuthError("Feishu OAuth returned an invalid response")
        code_value = body.get("code")
        try:
            code = int(code_value) if code_value is not None else None
        except (TypeError, ValueError):
            code = None
        if response.status_code >= 400 or code not in (None, 0):
            message = (
                body.get("msg")
                or body.get("error_description")
                or body.get("error")
                or "OAuth request failed"
            )
            raise OAuthError(
                f"Feishu OAuth rejected the request: {message}",
                code=code,
                status_code=response.status_code,
            )
        data = body.get("data")
        return data if isinstance(data, dict) else body


class StoredUserTokenProvider:
    """Callable user-token provider with atomic refresh-token rotation."""

    def __init__(
        self,
        oauth: OAuthClient,
        credentials: CredentialStore,
        *,
        credential_key: str = USER_TOKEN_CREDENTIAL,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.oauth = oauth
        self.credentials = credentials
        self.credential_key = credential_key
        self._now = now
        self._lock = threading.RLock()

    def begin_authorization(
        self, redirect_uri: str, *, scopes: Iterable[str] | None = None
    ) -> OAuthAuthorization:
        return self.oauth.begin_authorization(redirect_uri, scopes=scopes)

    def complete_authorization(self, *, code: str, state: str) -> TokenBundle:
        bundle = self.oauth.complete_authorization(code=code, state=state)
        self._save(bundle)
        return bundle

    def load(self) -> TokenBundle | None:
        value = self.credentials.get(self.credential_key)
        return TokenBundle.from_json(value) if value else None

    def clear(self) -> None:
        self.credentials.delete(self.credential_key)

    def __call__(self) -> str:
        with self._lock:
            bundle = self.load()
            if bundle is None:
                raise OAuthError("Feishu user authorization is not configured")
            bundle.require_scopes(self.oauth.required_scopes)
            if not bundle.access_valid(now=self._now()):
                bundle = self.oauth.refresh(bundle)
                # Persist the rotated refresh token before releasing the lock.
                self._save(bundle)
            return bundle.access_token

    def _save(self, bundle: TokenBundle) -> None:
        self.credentials.set(self.credential_key, bundle.to_json())


def save_app_secret(
    credentials: CredentialStore,
    app_secret: str,
    *,
    credential_key: str = APP_SECRET_CREDENTIAL,
) -> None:
    if not app_secret:
        raise ValueError("app_secret is required")
    credentials.set(credential_key, app_secret)


def stored_app_secret_provider(
    credentials: CredentialStore,
    *,
    credential_key: str = APP_SECRET_CREDENTIAL,
) -> Callable[[], str]:
    def provider() -> str:
        return credentials.get(credential_key) or ""

    return provider
