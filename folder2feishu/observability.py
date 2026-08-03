from __future__ import annotations

import contextvars
import math
import os
import threading
from collections import defaultdict, deque
from time import monotonic
from typing import Any

import psutil  # type: ignore[import-untyped]

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
task_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("task_id", default="")
stage_var: contextvars.ContextVar[str] = contextvars.ContextVar("stage", default="")


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


class RuntimeMetrics:
    """Small rolling metrics registry for this localhost-only application."""

    def __init__(self, *, window: int = 2_000) -> None:
        self._lock = threading.Lock()
        self._api: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self._api_errors = 0
        self._http: deque[float] = deque(maxlen=window)
        self._http_active = 0
        self._http_errors = 0
        self._http_timeouts = 0
        self._http_retries = 0
        self._started = monotonic()
        self._process = psutil.Process(os.getpid())

    def record_api(self, path: str, seconds: float, status_code: int) -> None:
        with self._lock:
            self._api[path].append(max(0.0, seconds))
            self._api_errors += int(status_code >= 500)

    def http_started(self) -> None:
        with self._lock:
            self._http_active += 1

    def http_finished(self, seconds: float, *, error: bool = False, timeout: bool = False) -> None:
        with self._lock:
            self._http_active = max(0, self._http_active - 1)
            self._http.append(max(0.0, seconds))
            self._http_errors += int(error)
            self._http_timeouts += int(timeout)

    def http_retried(self) -> None:
        with self._lock:
            self._http_retries += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            api_values = [value for values in self._api.values() for value in values]
            http_values = list(self._http)
            api_errors = self._api_errors
            http_active = self._http_active
            http_errors = self._http_errors
            http_timeouts = self._http_timeouts
            http_retries = self._http_retries
        memory = self._process.memory_info()
        io = self._process.io_counters()
        return {
            "uptime_seconds": int(monotonic() - self._started),
            "api": {
                "average_ms": round(sum(api_values) / len(api_values) * 1000, 2)
                if api_values
                else 0,
                "p95_ms": round(_percentile(api_values, 0.95) * 1000, 2),
                "p99_ms": round(_percentile(api_values, 0.99) * 1000, 2),
                "samples": len(api_values),
                "errors": api_errors,
            },
            "feishu_http": {
                "average_ms": round(sum(http_values) / len(http_values) * 1000, 2)
                if http_values
                else 0,
                "p95_ms": round(_percentile(http_values, 0.95) * 1000, 2),
                "active": http_active,
                "errors": http_errors,
                "timeouts": http_timeouts,
                "retries": http_retries,
            },
            "process": {
                "cpu_percent": self._process.cpu_percent(interval=None),
                "memory_rss_bytes": memory.rss,
                "threads": self._process.num_threads(),
                "open_files": len(self._process.open_files()),
                "connections": len(self._process.net_connections(kind="inet")),
                "disk_read_bytes": io.read_bytes,
                "disk_write_bytes": io.write_bytes,
            },
        }


METRICS = RuntimeMetrics()
