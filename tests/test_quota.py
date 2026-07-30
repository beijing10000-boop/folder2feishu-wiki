from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from folder2feishu.quota import DailyQuotaExceeded, DailyQuotaStore


def test_quota_is_durable_and_resets_on_next_day(tmp_path: Path) -> None:
    clock = {"now": datetime(2026, 7, 30, 23, 0)}
    path = tmp_path / "quota.json"
    quota = DailyQuotaStore(path, budget=10, now=lambda: clock["now"])
    assert quota.reserve(8).used == 8
    again = DailyQuotaStore(path, budget=10, now=lambda: clock["now"])
    with pytest.raises(DailyQuotaExceeded):
        again.reserve(3)
    clock["now"] = datetime(2026, 7, 31, 0, 1)
    assert again.snapshot().used == 0
    assert again.reserve(3).used == 3


def test_quota_release_never_goes_negative(tmp_path: Path) -> None:
    quota = DailyQuotaStore(tmp_path / "quota.json", budget=10)
    quota.reserve(2)
    assert quota.release(20).used == 0


def test_zero_budget_records_usage_without_stopping(tmp_path: Path) -> None:
    quota = DailyQuotaStore(tmp_path / "quota.json", budget=0)
    assert quota.reserve(100_000).used == 100_000
    assert quota.snapshot().budget == 0


def test_two_store_instances_share_one_atomic_process_lock(tmp_path: Path) -> None:
    path = tmp_path / "shared-quota.json"
    stores = [DailyQuotaStore(path, budget=1_000) for _ in range(2)]
    workers = 8
    calls_per_worker = 25
    start = threading.Barrier(workers)

    def reserve_many(worker: int) -> None:
        start.wait()
        store = stores[worker % len(stores)]
        for _ in range(calls_per_worker):
            store.reserve(1)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(reserve_many, range(workers)))

    assert stores[0].snapshot().used == workers * calls_per_worker
