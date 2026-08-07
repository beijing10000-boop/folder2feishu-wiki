"""Regression tests for silent-failure bugs in the retry, throttle and match paths.

Each test here pins behaviour that used to be wrong in a way no existing test
could observe: the code returned the right value, it just did it at the wrong
time or against the wrong remote object.
"""

from __future__ import annotations

import threading
import time

import pytest

from folder2feishu.core.enums import ItemKind, RemoteStatus
from folder2feishu.core.models import InventoryItem, RemoteMapping
from folder2feishu.core.planner import MigrationPlanner
from folder2feishu.feishu.rate_limit import IntervalRateLimiter
from folder2feishu.job_control import JobControl, JobStopped


class TestJobControlWait:
    """JobControl.wait backs every retry/backoff delay in FeishuAPIClient."""

    def test_wait_actually_elapses(self) -> None:
        # Regression: the loop called Event.wait() on an already-set event, so
        # the whole backoff collapsed to zero and 8 retries fired in one burst.
        control = JobControl()
        started = time.monotonic()
        control.wait(0.4, interval=0.1)
        assert time.monotonic() - started == pytest.approx(0.4, abs=0.15)

    def test_stop_interrupts_a_long_wait(self) -> None:
        control = JobControl()
        threading.Timer(0.2, control.stop).start()
        started = time.monotonic()
        with pytest.raises(JobStopped):
            control.wait(30.0)
        assert time.monotonic() - started < 2.0

    def test_pause_does_not_consume_the_backoff_budget(self) -> None:
        control = JobControl()
        control.pause()
        threading.Timer(0.3, control.resume).start()
        started = time.monotonic()
        control.wait(0.3, interval=0.05)
        # Paused time is held, then the full backoff still runs.
        assert time.monotonic() - started >= 0.55

    def test_zero_and_negative_waits_return_immediately(self) -> None:
        control = JobControl()
        started = time.monotonic()
        control.wait(0.0)
        control.wait(-5.0)
        assert time.monotonic() - started < 0.1


class TestIntervalRateLimiter:
    def test_defer_is_not_blocked_by_a_sleeping_acquirer(self) -> None:
        # Regression: acquire() slept while holding the lock, so a worker in a
        # 300s server cooldown froze defer() and every other caller with it.
        limiter = IntervalRateLimiter(1, 1)
        limiter.acquire()
        limiter.defer(3.0)
        waiter = threading.Thread(target=limiter.acquire, daemon=True)
        waiter.start()
        time.sleep(0.3)

        started = time.monotonic()
        limiter.defer(0.5)
        assert time.monotonic() - started < 0.2
        waiter.join(timeout=10)

    def test_spacing_is_still_enforced(self) -> None:
        clock = {"now": 0.0}
        slept: list[float] = []

        def monotonic() -> float:
            return clock["now"]

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            clock["now"] += seconds

        limiter = IntervalRateLimiter(5, 1, monotonic=monotonic, sleeper=sleeper)
        for _ in range(3):
            limiter.acquire()
        assert slept == [pytest.approx(0.2), pytest.approx(0.2)]


def _item(rel_path: str, *, identity: str, sha256: str | None) -> InventoryItem:
    return InventoryItem(
        id=f"item-{rel_path}",
        project_id="prj",
        rel_path=rel_path,
        name=rel_path.rsplit("/", 1)[-1],
        kind=ItemKind.FILE,
        size=len(sha256 or ""),
        file_identity=identity,
        sha256=sha256,
    )


def _mapping(rel_path: str, *, identity: str, sha256: str | None) -> RemoteMapping:
    return RemoteMapping(
        id=f"map-{rel_path}",
        project_id="prj",
        item_kind=ItemKind.FILE,
        last_source_rel_path=rel_path,
        source_file_identity=identity,
        source_sha256=sha256,
        remote_status=RemoteStatus.ACTIVE,
    )


class TestFileIdentityReuse:
    """The OS reuses an inode / NTFS File ID right after a delete."""

    def test_new_file_inheriting_a_deleted_files_identity_is_not_matched(self) -> None:
        # Without the guard this pairs an unrelated new file with the deleted
        # file's remote object and plans a VERSION_UPDATE over it.
        reused = "vol1:4242"
        items = [_item("A/new.txt", identity=reused, sha256="bbbb")]
        mappings = [_mapping("A/missing.txt", identity=reused, sha256="aaaa")]

        matches, ambiguity = MigrationPlanner._match(items, mappings)

        assert matches["item-A/new.txt"] is None
        assert not ambiguity

    def test_a_genuine_move_still_matches_by_identity(self) -> None:
        # Same identity and unchanged content: the plan can only move or
        # rename the remote object, so the match stays trustworthy.
        identity = "vol1:99"
        items = [_item("B/moved.txt", identity=identity, sha256="same")]
        mappings = [_mapping("A/moved.txt", identity=identity, sha256="same")]

        matches, _ = MigrationPlanner._match(items, mappings)

        assert matches["item-B/moved.txt"] is mappings[0]

    def test_edit_in_place_still_matches_by_path(self) -> None:
        # Path matching runs first, so an ordinary edit is unaffected.
        items = [_item("A/edit.txt", identity="vol1:7", sha256="new")]
        mappings = [_mapping("A/edit.txt", identity="vol1:7", sha256="old")]

        matches, _ = MigrationPlanner._match(items, mappings)

        assert matches["item-A/edit.txt"] is mappings[0]

    @pytest.mark.parametrize(
        ("item_digest", "mapping_digest"),
        [(None, "known"), ("known", None), (None, None)],
    )
    def test_missing_digest_never_trusts_a_reused_identity(
        self,
        item_digest: str | None,
        mapping_digest: str | None,
    ) -> None:
        identity = "vol1:reused"
        items = [_item("B/new.txt", identity=identity, sha256=item_digest)]
        mappings = [_mapping("A/missing.txt", identity=identity, sha256=mapping_digest)]

        matches, ambiguity = MigrationPlanner._match(items, mappings)

        assert matches["item-B/new.txt"] is None
        assert not ambiguity
