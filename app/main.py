from __future__ import annotations

import html
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import load_settings, public_settings, save_credentials
from .db import Database
from .feishu import FeishuError, FeishuWikiClient, UserTokenStore, oauth_authorize_url
from .migrator import MigrationManager, SourceScanner


STATIC_DIR = Path(__file__).resolve().parent / "static"
settings = load_settings()
db = Database(settings.database_file)


def client_factory() -> FeishuWikiClient:
    current = load_settings()
    if not current.configured:
        raise RuntimeError("请先保存飞书 App ID、App Secret 和 OAuth 回调地址")
    return FeishuWikiClient(current)


scanner = SourceScanner(db)
manager = MigrationManager(db, client_factory)
app = FastAPI(
    title="Folder2Feishu Wiki",
    version=__version__,
    docs_url=None,
    redoc_url=None,
)


class ConfigPayload(BaseModel):
    app_id: str = Field(min_length=1)
    app_secret: str = ""
    redirect_uri: str = Field(min_length=1)


class ScanPayload(BaseModel):
    path: str = Field(min_length=1)
    reset: bool = False


class TargetPayload(BaseModel):
    wiki_url: str = Field(min_length=1)
    create_wrapper: bool = True


@app.exception_handler(FeishuError)
async def feishu_error_handler(_request: Request, exc: FeishuError):
    detail = str(exc)
    if exc.log_id:
        detail += f"；log_id={exc.log_id}"
    return JSONResponse(
        status_code=400,
        content={"detail": detail},
    )


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
def api_state():
    current = load_settings()
    token_store = UserTokenStore(current)
    return {
        "version": __version__,
        "config": public_settings(current),
        "oauth": {
            "authorized": token_store.valid() if current.configured else False,
            "token_file": str(current.token_file),
        },
        "source": {
            "root": db.get_meta("source_root", ""),
            "root_name": db.get_meta("source_root_name", ""),
            "scan_started_at": db.get_meta("scan_started_at", ""),
            "scan_finished_at": db.get_meta("scan_finished_at", ""),
        },
        "target": db.get_meta("target", {}),
        "summary": db.summary(),
        "job": manager.status(),
        "events": db.events(80),
        "failures": db.failures(100),
    }


@app.post("/api/config")
def api_config(payload: ConfigPayload):
    current = load_settings()
    if not payload.app_secret.strip() and not current.app_secret:
        raise HTTPException(status_code=400, detail="首次配置必须填写 App Secret")
    saved = save_credentials(
        payload.app_id,
        payload.app_secret,
        payload.redirect_uri,
    )
    db.add_event("success", "飞书应用配置已保存")
    return public_settings(saved)


@app.get("/oauth/login", include_in_schema=False)
def oauth_login():
    current = load_settings()
    try:
        url, state = oauth_authorize_url(current)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.set_meta(
        "oauth_state",
        {"value": state, "expires_at": time.time() + 600},
    )
    return RedirectResponse(url)


@app.get("/oauth/callback", include_in_schema=False)
def oauth_callback(code: str = "", state: str = "", error: str = ""):
    expected = db.get_meta("oauth_state", {})
    if error:
        return HTMLResponse(
            f"<h2>飞书授权失败</h2><p>{html.escape(error)}</p><p><a href='/'>返回控制台</a></p>",
            status_code=400,
        )
    if (
        not code
        or not state
        or state != expected.get("value")
        or time.time() > float(expected.get("expires_at") or 0)
    ):
        return HTMLResponse(
            "<h2>飞书授权失败</h2><p>OAuth state 无效或已过期，请返回重新授权。</p>",
            status_code=400,
        )
    current = load_settings()
    try:
        UserTokenStore(current).exchange(code)
    except Exception as exc:
        return HTMLResponse(
            f"<h2>飞书授权失败</h2><p>{html.escape(str(exc))}</p><p><a href='/'>返回控制台</a></p>",
            status_code=400,
        )
    db.set_meta("oauth_state", {})
    db.add_event("success", "飞书 OAuth 授权完成")
    return RedirectResponse("/?oauth=success")


@app.post("/api/scan")
def api_scan(payload: ScanPayload):
    job = manager.status()
    if job.get("alive"):
        raise HTTPException(status_code=409, detail="迁移运行中，不能重新扫描")
    try:
        return scanner.scan(
            payload.path,
            reset=payload.reset,
            progress=lambda done, rel: db.set_meta(
                "scan_progress", {"done": done, "current": rel}
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/target")
def api_target(payload: TargetPayload):
    try:
        return manager.configure_target(
            payload.wiki_url,
            create_wrapper=payload.create_wrapper,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/migration/start")
def api_start():
    try:
        return manager.start()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/migration/pause")
def api_pause():
    return manager.pause()


@app.post("/api/migration/resume")
def api_resume():
    try:
        return manager.resume()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/migration/stop")
def api_stop():
    return manager.stop()


@app.post("/api/migration/retry")
def api_retry():
    return manager.retry_failed()


@app.post("/api/reset")
def api_reset(payload: dict = Body(default={})):
    if str((payload or {}).get("confirm") or "") != "RESET":
        raise HTTPException(status_code=400, detail="重置确认文字不正确")
    if manager.status().get("alive"):
        raise HTTPException(status_code=409, detail="请先停止迁移任务")
    db.reset_inventory()
    db.add_event("warning", "本地迁移台账已重置；飞书内容未删除")
    return {"ok": True}


@app.get("/api/health")
def api_health():
    return {
        "ok": True,
        "version": __version__,
        "database": str(load_settings().database_file),
    }
