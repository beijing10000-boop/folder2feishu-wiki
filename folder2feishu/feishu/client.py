from __future__ import annotations

import email.utils
import random
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from .errors import (
    FeishuAmbiguousWriteError,
    FeishuAPIError,
    FeishuProtocolError,
    FeishuTransportError,
)
from .models import RetryMode
from .rate_limit import RateLimitSet

OPEN_API_BASE = "https://open.feishu.cn/open-apis"
RETRYABLE_CODES = frozenset({1061045, 99991400, 99991401, 99991402})
RATE_LIMIT_RETRYABLE_CODES = frozenset({1061045})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class UserAccessTokenProvider(Protocol):
    def __call__(self) -> str: ...


def _file_positions(files: Any) -> list[tuple[Any, int]]:
    if not isinstance(files, Mapping):
        return []
    positions: list[tuple[Any, int]] = []
    for value in files.values():
        candidate = value
        if isinstance(value, tuple):
            candidate = next(
                (
                    item
                    for item in reversed(value)
                    if hasattr(item, "seek") and hasattr(item, "tell")
                ),
                None,
            )
        if candidate is not None and hasattr(candidate, "seek") and hasattr(candidate, "tell"):
            try:
                positions.append((candidate, int(candidate.tell())))
            except (OSError, ValueError):
                continue
    return positions


class FeishuAPIClient:
    """Authenticated, rate-limited HTTPX client for Feishu OpenAPI.

    A single user-token provider is fixed at construction time. Individual API
    methods never accept a raw token, preventing accidental identity switching
    between Drive upload and Wiki move/poll operations.
    """

    def __init__(
        self,
        token_provider: UserAccessTokenProvider,
        *,
        client: httpx.Client | None = None,
        base_url: str = OPEN_API_BASE,
        rate_limits: RateLimitSet | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        max_attempts: int = 5,
        base_delay: float = 0.5,
        max_delay: float = 30,
    ) -> None:
        if not base_url.startswith("https://") and client is None:
            raise ValueError("Feishu base_url must use HTTPS")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._token_provider = token_provider
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(120, connect=20),
            follow_redirects=False,
        )
        self.base_url = base_url.rstrip("/")
        self.rate_limits = rate_limits or RateLimitSet()
        self._sleep = sleeper
        self._random = random_value
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        rate_group: str = RateLimitSet.GENERAL,
        retry_mode: RetryMode = RetryMode.SAFE,
        before_attempt: Callable[[], None] | None = None,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        method = method.upper()
        if not path.startswith("/"):
            raise ValueError("OpenAPI path must start with '/'")
        if retry_mode == RetryMode.SAFE and method not in SAFE_METHODS:
            retry_mode = RetryMode.NEVER

        supplied_headers = dict(headers or {})
        # Never permit a caller to switch identity through an injected header.
        supplied_headers.pop("Authorization", None)
        files = kwargs.get("files")
        positions = _file_positions(files)
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                for stream, position in positions:
                    try:
                        stream.seek(position)
                    except (OSError, ValueError) as exc:
                        raise FeishuTransportError(
                            "unable to rewind upload stream for a safe retry"
                        ) from exc
            self.rate_limits.acquire(rate_group)
            token = self._token_provider()
            if not token:
                raise FeishuTransportError("user access token provider returned empty")
            if before_attempt is not None:
                before_attempt()
            request_headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                **supplied_headers,
            }
            try:
                response = self.client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=request_headers,
                    **kwargs,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if retry_mode == RetryMode.ALWAYS and attempt < self.max_attempts:
                    self._sleep(self._backoff(attempt, None))
                    continue
                if method not in SAFE_METHODS:
                    raise FeishuAmbiguousWriteError(
                        "Feishu write outcome is unknown; reconcile remote state before retrying"
                    ) from exc
                raise FeishuTransportError("unable to reach Feishu OpenAPI") from exc

            try:
                body = self._decode(response)
                return body
            except FeishuProtocolError as exc:
                if (
                    retry_mode == RetryMode.RATE_LIMIT
                    and method not in SAFE_METHODS
                    and response.status_code >= 500
                ):
                    raise FeishuAmbiguousWriteError(
                        "Feishu write returned an ambiguous non-JSON server failure; "
                        "reconcile remote state before retrying"
                    ) from exc
                raise
            except FeishuAPIError as exc:
                last_error = exc
                can_retry_server = (
                    retry_mode
                    in {
                        RetryMode.SAFE,
                        RetryMode.SERVER,
                        RetryMode.ALWAYS,
                    }
                    and exc.retryable
                )
                can_retry_rate_limit = retry_mode == RetryMode.RATE_LIMIT and (
                    exc.status_code == 429 or exc.code in RATE_LIMIT_RETRYABLE_CODES
                )
                if (can_retry_server or can_retry_rate_limit) and attempt < self.max_attempts:
                    self._sleep(self._backoff(attempt, response))
                    continue
                if (
                    retry_mode == RetryMode.RATE_LIMIT
                    and method not in SAFE_METHODS
                    and exc.retryable
                    and not can_retry_rate_limit
                ):
                    raise FeishuAmbiguousWriteError(
                        "Feishu write returned an ambiguous server failure; "
                        "reconcile remote state before retrying"
                    ) from exc
                raise

        raise FeishuTransportError(f"Feishu request failed after retries: {last_error}")

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        log_id = response.headers.get("x-tt-logid", "")
        try:
            body = response.json()
        except ValueError as exc:
            raise FeishuProtocolError(
                f"Feishu returned non-JSON HTTP {response.status_code}"
            ) from exc
        if not isinstance(body, dict):
            raise FeishuProtocolError("Feishu returned a non-object JSON response")
        raw_code = body.get("code")
        try:
            code = int(raw_code) if raw_code is not None else None
        except (TypeError, ValueError):
            code = None
        failed = response.status_code >= 400 or code not in (None, 0)
        if failed:
            retryable = (
                response.status_code == 429
                or response.status_code >= 500
                or code in RETRYABLE_CODES
            )
            message = (
                body.get("msg")
                or body.get("message")
                or response.reason_phrase
                or "OpenAPI request failed"
            )
            marker = code if code is not None else response.status_code
            raise FeishuAPIError(
                f"Feishu OpenAPI error [{marker}]: {message}",
                code=code,
                status_code=response.status_code,
                log_id=log_id,
                retryable=retryable,
            )
        return body

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        retry_after = self._retry_after(response) if response is not None else None
        if retry_after is not None:
            return min(self.max_delay, max(0.0, retry_after))
        exponential = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        # Add up to 25% jitter to avoid synchronized retries across projects.
        return exponential * (1 + 0.25 * self._random())

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            try:
                retry_at = email.utils.parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError):
                return None
