from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "Folder2FeishuWiki"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """All mutable application data lives outside the source tree."""

    base: Path
    database: Path
    settings: Path
    credentials: Path
    quota: Path
    logs: Path
    exports: Path

    @classmethod
    def discover(cls, override: str | Path | None = None) -> RuntimePaths:
        if override:
            base = Path(override).expanduser().resolve()
        elif configured := os.environ.get("FOLDER2FEISHU_HOME"):
            base = Path(configured).expanduser().resolve()
        elif local_app_data := os.environ.get("LOCALAPPDATA"):
            base = (Path(local_app_data) / APP_DIR_NAME).resolve()
        else:
            base = (Path.home() / ".folder2feishu-wiki").resolve()
        return cls(
            base=base,
            database=base / "ledger.sqlite3",
            settings=base / "settings.json",
            credentials=base / "credentials.bin",
            quota=base / "quota.json",
            logs=base / "logs",
            exports=base / "exports",
        )

    def ensure(self) -> RuntimePaths:
        self.base.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.exports.mkdir(parents=True, exist_ok=True)
        return self


def bundled_path(*parts: str) -> Path:
    """Resolve an immutable asset both from source and from PyInstaller."""

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        root = Path(sys._MEIPASS)
    else:
        root = Path(__file__).resolve().parents[1]
    return root.joinpath(*parts)


def assert_runtime_outside_source(runtime: RuntimePaths, source_root: str | Path) -> None:
    """Refuse a ledger inside the OneDrive/source tree.

    Writing the SQLite ledger under a synchronized source can produce both
    database corruption and accidental uploads of the ledger itself.
    """

    source = Path(source_root).expanduser().resolve()
    runtime_base = runtime.base.resolve()
    if runtime_base == source or source in runtime_base.parents:
        raise ValueError("应用数据目录不能位于迁移源目录或 OneDrive 同步目录内")
