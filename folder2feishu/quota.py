from __future__ import annotations

import importlib
import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _shared_path_lock(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@contextmanager
def _interprocess_path_lock(path: Path) -> Iterator[None]:
    """Serialize quota read-modify-write across GUI and scheduled processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class DailyQuotaExceeded(RuntimeError):
    def __init__(self, used: int, budget: int, reset_at: datetime) -> None:
        super().__init__(f"今日上传调用预算已用完：{used}/{budget}")
        self.used = used
        self.budget = budget
        self.reset_at = reset_at


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    day: str
    used: int
    budget: int
    reset_at: datetime


class DailyQuotaStore:
    """Conservative local budget below Feishu's published 10,000/day cap."""

    def __init__(
        self,
        path: str | Path,
        *,
        budget: int = 9_500,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        if not 1 <= budget <= 9_500:
            raise ValueError("每日调用预算必须在 1 到 9500 之间")
        self.path = Path(path)
        self.budget = budget
        self._now = now
        self._lock = _shared_path_lock(self.path)

    def snapshot(self) -> QuotaSnapshot:
        with self._lock, _interprocess_path_lock(self.path):
            return self._snapshot_unlocked()

    def reserve(self, calls: int) -> QuotaSnapshot:
        if calls < 0:
            raise ValueError("调用次数不能为负数")
        with self._lock, _interprocess_path_lock(self.path):
            current = self._snapshot_unlocked()
            if current.used + calls > current.budget:
                raise DailyQuotaExceeded(current.used, current.budget, current.reset_at)
            updated = QuotaSnapshot(
                day=current.day,
                used=current.used + calls,
                budget=current.budget,
                reset_at=current.reset_at,
            )
            self._write({"day": updated.day, "used": updated.used})
            return updated

    def release(self, calls: int) -> QuotaSnapshot:
        """Release calls that were reserved but never sent to Feishu."""

        if calls < 0:
            raise ValueError("调用次数不能为负数")
        with self._lock, _interprocess_path_lock(self.path):
            current = self._snapshot_unlocked()
            updated = QuotaSnapshot(
                day=current.day,
                used=max(0, current.used - calls),
                budget=current.budget,
                reset_at=current.reset_at,
            )
            self._write({"day": updated.day, "used": updated.used})
            return updated

    def _snapshot_unlocked(self) -> QuotaSnapshot:
        current = self._now()
        day = current.date().isoformat()
        value = self._read()
        raw_used = value.get("used", 0)
        used = int(raw_used) if value.get("day") == day and isinstance(raw_used, str | int) else 0
        reset_at = datetime.combine(
            current.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=current.tzinfo,
        )
        return QuotaSnapshot(day, used, self.budget, reset_at)

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, value: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.path)
