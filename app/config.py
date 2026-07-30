from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv, set_key


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    redirect_uri: str
    oauth_scope: str
    host: str
    port: int
    data_dir: Path

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret and self.redirect_uri)

    @property
    def token_file(self) -> Path:
        return self.data_dir / "oauth_token.json"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "migration.db"


def load_settings() -> Settings:
    load_dotenv(ENV_FILE, override=True)
    data_dir = Path(os.getenv("APP_DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser()
    if not data_dir.is_absolute():
        data_dir = (PROJECT_ROOT / data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_id=os.getenv("FEISHU_APP_ID", "").strip(),
        app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
        redirect_uri=os.getenv(
            "FEISHU_REDIRECT_URI", "http://localhost:8765/oauth/callback"
        ).strip(),
        oauth_scope=os.getenv(
            "FEISHU_OAUTH_SCOPE", "wiki:wiki drive:drive offline_access"
        ).strip(),
        host=os.getenv("APP_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=int(os.getenv("APP_PORT", "8765")),
        data_dir=data_dir,
    )


def save_credentials(app_id: str, app_secret: str, redirect_uri: str) -> Settings:
    ENV_FILE.touch(exist_ok=True)
    set_key(str(ENV_FILE), "FEISHU_APP_ID", app_id.strip(), quote_mode="always")
    if app_secret.strip():
        set_key(
            str(ENV_FILE),
            "FEISHU_APP_SECRET",
            app_secret.strip(),
            quote_mode="always",
        )
    set_key(
        str(ENV_FILE),
        "FEISHU_REDIRECT_URI",
        redirect_uri.strip(),
        quote_mode="always",
    )
    return load_settings()


def public_settings(settings: Settings) -> dict:
    return {
        "app_id": settings.app_id,
        "app_secret_configured": bool(settings.app_secret),
        "redirect_uri": settings.redirect_uri,
        "oauth_scope": settings.oauth_scope,
        "configured": settings.configured,
    }

