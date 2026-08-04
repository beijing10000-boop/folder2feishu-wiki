from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol


class RateLimiter(Protocol):
    def acquire(self) -> None: ...

    def defer(self, seconds: float) -> None: ...


class NoopRateLimiter:
    def acquire(self) -> None:
        return

    def defer(self, seconds: float) -> None:
        del seconds


class IntervalRateLimiter:
    """Thread-safe evenly-spaced limiter.

    Even spacing avoids bursts: 5 QPS means one call every 200 ms and
    100/min means one call every 600 ms.
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

    def defer(self, seconds: float) -> None:
        """Apply a server-requested cooldown to every caller in this bucket."""

        if seconds <= 0:
            return
        with self._lock:
            self._next_allowed = max(
                self._next_allowed,
                self._monotonic() + float(seconds),
            )


class RateLimitSet:
    DRIVE_UPLOAD = "drive_upload"
    DRIVE_WRITE = "drive_write"
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
        drive_write: RateLimiter | None = None,
        wiki: RateLimiter | None = None,
        wiki_create: RateLimiter | None = None,
        wiki_read: RateLimiter | None = None,
        wiki_write: RateLimiter | None = None,
        general: RateLimiter | None = None,
    ) -> None:
        shared_drive = drive_upload or IntervalRateLimiter(5, 1)
        legacy_wiki = wiki or IntervalRateLimiter(100, 60)
        self._groups: dict[str, RateLimiter] = {
            self.DRIVE_UPLOAD: shared_drive,
            self.DRIVE_WRITE: drive_write or shared_drive,
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
            drive_write=noop,
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

    def defer(self, group: str, seconds: float) -> None:
        """Stop the whole endpoint bucket after Feishu reports a cooldown.

        Without a shared cooldown, one worker sleeps while the remaining
        workers continue hitting the same server-side window and turn a
        single 429/99991400 into thousands of retryable failures.
        """

        try:
            limiter = self._groups[group]
        except KeyError as exc:
            raise ValueError(f"unknown rate-limit group: {group}") from exc
        limiter.defer(seconds)
