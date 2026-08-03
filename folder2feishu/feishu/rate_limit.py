from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol


class RateLimiter(Protocol):
    def acquire(self) -> None: ...


class NoopRateLimiter:
    def acquire(self) -> None:
        return


class IntervalRateLimiter:
    """Thread-safe evenly-spaced limiter.

    Even spacing is intentionally conservative: 4 QPS means one call every
    250 ms and 90/min means one call every 2/3 second.
    """

    def __init__(
        self,
        requests: float,
        period_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests <= 0 or period_seconds <= 0:
            raise ValueError("rate limit values must be positive")
        self._interval = period_seconds / requests
        self._monotonic = monotonic
        self._sleep = sleeper
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._monotonic()
            delay = max(0.0, self._next_allowed - now)
            if delay:
                self._sleep(delay)
                now = max(self._monotonic(), self._next_allowed)
            self._next_allowed = max(now, self._next_allowed) + self._interval


class RateLimitSet:
    DRIVE_UPLOAD = "drive_upload"
    WIKI_CREATE = "wiki_create"
    WIKI_READ = "wiki_read"
    WIKI_WRITE = "wiki_write"
    # Backward-compatible alias used by callers outside the v2 services.
    WIKI = "wiki"
    GENERAL = "general"

    def __init__(
        self,
        *,
        drive_upload: RateLimiter | None = None,
        wiki: RateLimiter | None = None,
        wiki_create: RateLimiter | None = None,
        wiki_read: RateLimiter | None = None,
        wiki_write: RateLimiter | None = None,
        general: RateLimiter | None = None,
    ) -> None:
        legacy_wiki = wiki or IntervalRateLimiter(90, 60)
        self._groups: dict[str, RateLimiter] = {
            self.DRIVE_UPLOAD: drive_upload or IntervalRateLimiter(4, 1),
            self.WIKI: legacy_wiki,
            self.WIKI_CREATE: wiki_create or legacy_wiki,
            self.WIKI_READ: wiki_read or legacy_wiki,
            self.WIKI_WRITE: wiki_write or legacy_wiki,
            self.GENERAL: general or IntervalRateLimiter(20, 1),
        }

    @classmethod
    def disabled(cls) -> RateLimitSet:
        noop = NoopRateLimiter()
        return cls(
            drive_upload=noop,
            wiki=noop,
            wiki_create=noop,
            wiki_read=noop,
            wiki_write=noop,
            general=noop,
        )

    def acquire(self, group: str) -> None:
        try:
            limiter = self._groups[group]
        except KeyError as exc:
            raise ValueError(f"unknown rate-limit group: {group}") from exc
        limiter.acquire()
