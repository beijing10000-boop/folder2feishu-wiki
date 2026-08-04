from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from .runtime import RuntimePaths

DEFAULT_SCOPES = (
    "offline_access",
    "drive:drive",
    "drive:file:upload",
    "drive:quota_detail:read_one",
    "contact:user.employee_id:readonly",
)


@dataclass(slots=True)
class PublicSettings:
    app_id: str = ""
    redirect_uri: str = "http://localhost:8000/oauth/callback"
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))
    host: str = "127.0.0.1"
    port: int = 8000
    upload_qps: float = 4.0
    wiki_calls_per_minute: int = 100  # 兼容旧接口；云盘版不再使用知识库限速器
    daily_upload_budget: int = 0
    open_browser: bool = True
    runtime_tuning_version: int = 2

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> PublicSettings:
        allowed = cls.__dataclass_fields__.keys()
        payload = {key: item for key, item in value.items() if key in allowed}
        # v1 used 90/min as a conservative default. The official node-create
        # limit is 100/min; migrate only that old default, while preserving any
        # deliberately lower custom value.
        if (
            int(value.get("runtime_tuning_version", 1)) < 2
            and int(payload.get("wiki_calls_per_minute", 90)) == 90
        ):
            payload["wiki_calls_per_minute"] = 100
        payload["runtime_tuning_version"] = 2
        # Older releases persisted a conservative 9,500-call application budget.
        # The migration worker now runs without an application-level daily cap.
        payload["daily_upload_budget"] = 0
        result = cls(**payload)
        result.validate()
        return result

    def validate(self) -> None:
        if self.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("服务只允许监听 127.0.0.1")
        if not 1 <= int(self.port) <= 65_535:
            raise ValueError("端口必须在 1 到 65535 之间")
        if not self.redirect_uri.startswith(("http://localhost:", "http://127.0.0.1:")):
            raise ValueError("OAuth 回调必须使用本机 localhost 地址")
        required = set(DEFAULT_SCOPES)
        if not required.issubset(set(self.scopes)):
            missing = ", ".join(sorted(required - set(self.scopes)))
            raise ValueError(f"缺少必需的飞书权限：{missing}")
        if not 0 < float(self.upload_qps) <= 4.0:
            raise ValueError("上传速率必须大于 0 且不超过 4 QPS")
        if not 1 <= int(self.wiki_calls_per_minute) <= 100:
            raise ValueError("知识库调用频率必须在 1 到 100 次/分钟之间")
        if int(self.daily_upload_budget) != 0:
            raise ValueError("应用侧调用限制必须为 0（不设累计总次数上限）")


class SettingsStore:
    """Thread-safe JSON store that never contains the app secret or OAuth token."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self._lock = threading.RLock()

    def load(self) -> PublicSettings:
        with self._lock:
            if not self.paths.settings.exists():
                return PublicSettings()
            raw = json.loads(self.paths.settings.read_text(encoding="utf-8"))
            return PublicSettings.from_mapping(raw)

    def save(self, settings: PublicSettings) -> None:
        settings.validate()
        self.paths.ensure()
        payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
        temporary = self.paths.settings.with_suffix(".tmp")
        with self._lock:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(self.paths.settings)
