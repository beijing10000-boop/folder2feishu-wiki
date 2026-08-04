from __future__ import annotations

import httpx
import pytest

from folder2feishu.feishu import (
    FeishuAmbiguousWriteError,
    FeishuAPIClient,
    IntervalRateLimiter,
    RateLimitSet,
    RetryMode,
)
from folder2feishu.quota import DailyQuotaStore


def test_retries_429_5xx_and_1061045_with_exponential_backoff():
    calls = []
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        attempt = len(calls)
        if attempt == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"code": 99991400, "msg": "rate limited"},
            )
        if attempt == 2:
            return httpx.Response(503, json={"code": 0, "msg": "temporary"})
        if attempt == 3:
            return httpx.Response(200, json={"code": 1061045, "msg": "retry"})
        return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

    api = FeishuAPIClient(
        lambda: "u-fixed-user",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limits=RateLimitSet.disabled(),
        sleeper=sleeps.append,
        random_value=lambda: 0,
        base_delay=0.5,
    )
    assert api.request("GET", "/test", retry_mode=RetryMode.SAFE)["data"]["ok"]
    assert len(calls) == 4
    assert sleeps == [0.0, 1.0, 2.0]
    assert all(request.headers["authorization"] == "Bearer u-fixed-user" for request in calls)


def test_interval_limiter_server_cooldown_blocks_the_whole_bucket():
    clock = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    limiter = IntervalRateLimiter(
        4,
        1,
        monotonic=lambda: clock[0],
        sleeper=sleep,
    )

    limiter.acquire()
    limiter.defer(5)
    limiter.acquire()
    limiter.acquire()

    assert sleeps == [5.0, 0.25]


def test_caller_cannot_override_fixed_user_identity():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json={"code": 0})

    api = FeishuAPIClient(
        lambda: "u-fixed",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limits=RateLimitSet.disabled(),
    )
    api.request("GET", "/test", headers={"Authorization": "Bearer t-other"})
    assert seen["authorization"] == "Bearer u-fixed"


def test_transport_failure_on_write_requires_remote_reconciliation():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("unknown outcome", request=request)

    api = FeishuAPIClient(
        lambda: "u-fixed",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limits=RateLimitSet.disabled(),
        sleeper=lambda _: None,
    )
    with pytest.raises(FeishuAmbiguousWriteError):
        api.request("POST", "/write", json={"value": 1}, retry_mode=RetryMode.SERVER)


@pytest.mark.parametrize(
    ("status_code", "code"),
    [(429, 99991400), (200, 1061045)],
)
def test_rate_limit_write_mode_retries_only_explicit_throttles(
    status_code,
    code,
    tmp_path,
):
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status_code, json={"code": code, "msg": "retry later"})
        return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

    api = FeishuAPIClient(
        lambda: "u-fixed",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limits=RateLimitSet.disabled(),
        sleeper=lambda _: None,
    )
    quota = DailyQuotaStore(tmp_path / f"quota-{status_code}-{code}.json", budget=10)

    def reserve_attempt() -> None:
        quota.reserve(1)

    result = api.request(
        "POST",
        "/write",
        json={"value": 1},
        retry_mode=RetryMode.RATE_LIMIT,
        before_attempt=reserve_attempt,
    )

    assert result["data"]["ok"] is True
    assert calls == 2
    assert quota.snapshot().used == 2


def test_legacy_400_rate_limit_honors_feishu_reset_and_defers_shared_bucket():
    calls = 0
    sleeps: list[float] = []

    class RecordingLimiter:
        def __init__(self):
            self.acquires = 0
            self.deferred: list[float] = []

        def acquire(self) -> None:
            self.acquires += 1

        def defer(self, seconds: float) -> None:
            self.deferred.append(seconds)

    limiter = RecordingLimiter()

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                400,
                headers={"x-ogw-ratelimit-reset": "52"},
                json={"code": 99991400, "msg": "request trigger frequency limit"},
            )
        return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

    limits = RateLimitSet(wiki=limiter)
    api = FeishuAPIClient(
        lambda: "u-fixed",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limits=limits,
        sleeper=sleeps.append,
        max_delay=1,
    )

    result = api.request(
        "POST",
        "/wiki/write",
        rate_group=RateLimitSet.WIKI_WRITE,
        retry_mode=RetryMode.RATE_LIMIT,
        json={"value": 1},
    )

    assert result["data"]["ok"] is True
    assert calls == 2
    assert limiter.acquires == 2
    assert limiter.deferred == [52.0]
    assert sleeps == [52.0]


def test_safe_read_rate_limit_also_defers_shared_bucket():
    calls = 0

    class RecordingLimiter:
        def __init__(self):
            self.deferred: list[float] = []

        def acquire(self) -> None:
            pass

        def defer(self, seconds: float) -> None:
            self.deferred.append(seconds)

    limiter = RecordingLimiter()

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                400,
                headers={"x-ogw-ratelimit-reset": "3"},
                json={"code": 99991400, "msg": "request trigger frequency limit"},
            )
        return httpx.Response(200, json={"code": 0})

    api = FeishuAPIClient(
        lambda: "u-fixed",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limits=RateLimitSet(wiki=limiter),
        sleeper=lambda _: None,
    )

    api.request("GET", "/wiki/read", rate_group=RateLimitSet.WIKI_READ)

    assert calls == 2
    assert limiter.deferred == [3.0]


def test_rate_limit_write_mode_never_reposts_5xx():
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"code": 500001, "msg": "ambiguous"})

    api = FeishuAPIClient(
        lambda: "u-fixed",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limits=RateLimitSet.disabled(),
        sleeper=lambda _: None,
    )

    with pytest.raises(FeishuAmbiguousWriteError):
        api.request(
            "POST",
            "/write",
            json={"value": 1},
            retry_mode=RetryMode.RATE_LIMIT,
        )

    assert calls == 1


def test_rate_limit_groups_are_independent():
    class Counter:
        def __init__(self):
            self.calls = 0

        def acquire(self):
            self.calls += 1

    drive, wiki, general = Counter(), Counter(), Counter()
    limits = RateLimitSet(drive_upload=drive, wiki=wiki, general=general)
    api = FeishuAPIClient(
        lambda: "u-fixed",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0}))
        ),
        rate_limits=limits,
    )
    api.request("GET", "/drive", rate_group=RateLimitSet.DRIVE_UPLOAD)
    api.request("GET", "/wiki", rate_group=RateLimitSet.WIKI)
    assert (drive.calls, wiki.calls, general.calls) == (1, 1, 0)


def test_drive_writes_and_wiki_operations_share_their_configured_buckets():
    class Counter:
        def __init__(self):
            self.calls = 0

        def acquire(self):
            self.calls += 1

        def defer(self, seconds):
            del seconds

    drive, wiki = Counter(), Counter()
    limits = RateLimitSet(drive_upload=drive, wiki=wiki)

    limits.acquire(RateLimitSet.DRIVE_UPLOAD)
    limits.acquire(RateLimitSet.DRIVE_WRITE)
    limits.acquire(RateLimitSet.WIKI_CREATE)
    limits.acquire(RateLimitSet.WIKI_READ)
    limits.acquire(RateLimitSet.WIKI_WRITE)

    assert drive.calls == 2
    assert wiki.calls == 3


def test_wiki_endpoint_rate_limit_groups_are_independent():
    class Counter:
        def __init__(self):
            self.calls = 0

        def acquire(self):
            self.calls += 1

    create, read, write = Counter(), Counter(), Counter()
    limits = RateLimitSet(
        wiki_create=create,
        wiki_read=read,
        wiki_write=write,
    )
    api = FeishuAPIClient(
        lambda: "u-fixed",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0}))
        ),
        rate_limits=limits,
    )

    api.request("POST", "/wiki/create", rate_group=RateLimitSet.WIKI_CREATE)
    api.request("GET", "/wiki/read", rate_group=RateLimitSet.WIKI_READ)
    api.request("POST", "/wiki/write", rate_group=RateLimitSet.WIKI_WRITE)

    assert (create.calls, read.calls, write.calls) == (1, 1, 1)
