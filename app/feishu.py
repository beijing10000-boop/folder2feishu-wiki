from __future__ import annotations

import json
import math
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings


OPEN_API = "https://open.feishu.cn/open-apis"
ACCOUNTS = "https://accounts.feishu.cn"
SINGLE_UPLOAD_LIMIT = 20 * 1024 * 1024
RETRYABLE_CODES = {1061045, 99991400, 99991401, 99991402}


class FeishuError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        status_code: int | None = None,
        log_id: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.log_id = log_id


def parse_wiki_token(value: str) -> str:
    raw = (value or "").strip()
    match = re.search(r"/wiki/([A-Za-z0-9_-]+)", raw)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", raw):
        return raw
    raise ValueError("请输入飞书知识库节点链接，或只填写 wiki node token")


def oauth_authorize_url(settings: Settings, state: str | None = None) -> tuple[str, str]:
    if not settings.configured:
        raise ValueError("请先配置飞书 App ID、App Secret 和回调地址")
    state = state or secrets.token_urlsafe(32)
    params = {
        "client_id": settings.app_id,
        "redirect_uri": settings.redirect_uri,
        "response_type": "code",
        "scope": settings.oauth_scope,
        "state": state,
    }
    return f"{ACCOUNTS}/open-apis/authen/v1/authorize?{urlencode(params)}", state


class UserTokenStore:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.path = settings.token_file
        self.client = client or httpx.Client(timeout=30)

    def save(self, token_data: dict) -> None:
        record = dict(token_data.get("data") or token_data)
        record["obtained_at"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.path)

    def exchange(self, code: str) -> dict:
        response = self.client.post(
            f"{ACCOUNTS}/oauth/v3/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.settings.app_id,
                "client_secret": self.settings.app_secret,
                "code": code,
                "redirect_uri": self.settings.redirect_uri,
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise FeishuError(
                data.get("error_description") or data.get("error") or "OAuth 换取令牌失败"
            )
        self.save(data)
        return data

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def valid(self) -> bool:
        try:
            return bool(self.access_token())
        except (FeishuError, httpx.HTTPError):
            return False

    def access_token(self) -> str:
        data = self._read()
        token = str(data.get("access_token") or "")
        obtained_at = float(data.get("obtained_at") or 0)
        expires_in = int(data.get("expires_in") or 0)
        if token and (not expires_in or time.time() < obtained_at + expires_in - 300):
            return token
        refresh_token = str(data.get("refresh_token") or "")
        if not refresh_token:
            raise FeishuError("飞书 OAuth 未授权或已失效，请重新授权")
        response = self.client.post(
            f"{ACCOUNTS}/oauth/v3/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.settings.app_id,
                "client_secret": self.settings.app_secret,
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        refreshed = response.json()
        if refreshed.get("error"):
            raise FeishuError(
                refreshed.get("error_description")
                or refreshed.get("error")
                or "刷新飞书 OAuth 令牌失败"
            )
        refreshed.setdefault("refresh_token", refresh_token)
        self.save(refreshed)
        token = str((refreshed.get("data") or refreshed).get("access_token") or "")
        if not token:
            raise FeishuError("刷新 OAuth 后未返回 access_token")
        return token


class FeishuWikiClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleeper=time.sleep,
    ):
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(120, connect=30))
        self.tokens = UserTokenStore(settings, self.client)
        self.sleep = sleeper

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens.access_token()}"}

    @staticmethod
    def _decode(response: httpx.Response) -> dict:
        log_id = response.headers.get("x-tt-logid", "")
        try:
            body = response.json()
        except ValueError as exc:
            raise FeishuError(
                f"飞书返回了非 JSON 响应（HTTP {response.status_code}）",
                status_code=response.status_code,
                log_id=log_id,
            ) from exc
        code = body.get("code")
        if response.status_code >= 400 or code not in (None, 0):
            message = body.get("msg") or body.get("message") or response.reason_phrase
            raise FeishuError(
                f"[{code if code is not None else response.status_code}] {message}",
                code=code,
                status_code=response.status_code,
                log_id=log_id,
            )
        return body

    def request(
        self,
        method: str,
        path: str,
        *,
        retry_safe: bool = False,
        max_attempts: int = 5,
        **kwargs: Any,
    ) -> dict:
        last: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.client.request(
                    method,
                    f"{OPEN_API}{path}",
                    headers={**self._headers(), **kwargs.pop("headers", {})},
                    **kwargs,
                )
                return self._decode(response)
            except FeishuError as exc:
                last = exc
                retryable = (
                    exc.status_code == 429
                    or exc.code in RETRYABLE_CODES
                    or (exc.status_code is not None and exc.status_code >= 500)
                )
                if not (retry_safe and retryable and attempt < max_attempts):
                    raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if not (retry_safe and attempt < max_attempts):
                    raise FeishuError(f"连接飞书失败：{exc}") from exc
            self.sleep(min(30, 2 ** (attempt - 1)))
        raise FeishuError(f"飞书请求失败：{last}")

    def get_node(self, node_token: str) -> dict:
        body = self.request(
            "GET",
            "/wiki/v2/spaces/get_node",
            params={"token": node_token},
            retry_safe=True,
        )
        return (body.get("data") or {}).get("node") or {}

    def list_children(self, space_id: str, parent_node_token: str) -> list[dict]:
        result: list[dict] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 50}
            if parent_node_token:
                params["parent_node_token"] = parent_node_token
            if page_token:
                params["page_token"] = page_token
            body = self.request(
                "GET",
                f"/wiki/v2/spaces/{space_id}/nodes",
                params=params,
                retry_safe=True,
            )
            data = body.get("data") or {}
            result.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                break
        return result

    def create_folder_node(
        self, space_id: str, title: str, parent_node_token: str
    ) -> dict:
        body = self.request(
            "POST",
            f"/wiki/v2/spaces/{space_id}/nodes",
            json={
                "obj_type": "docx",
                "node_type": "origin",
                "parent_node_token": parent_node_token,
                "title": title,
            },
        )
        node = (body.get("data") or {}).get("node") or {}
        if not node.get("node_token"):
            raise FeishuError("创建知识库目录节点成功，但响应中缺少 node_token")
        return node

    def upload_file(self, local_path: Path, file_name: str) -> str:
        size = local_path.stat().st_size
        if size <= 0:
            raise FeishuError("飞书不支持上传 0 字节文件")
        if size <= SINGLE_UPLOAD_LIMIT:
            return self._upload_all(local_path, file_name, size)
        return self._upload_chunked(local_path, file_name, size)

    def _upload_all(self, path: Path, name: str, size: int) -> str:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with path.open("rb") as stream:
                    body = self.request(
                        "POST",
                        "/drive/v1/files/upload_all",
                        data={
                            "file_name": name,
                            "parent_type": "explorer",
                            "parent_node": "",
                            "size": str(size),
                        },
                        files={"file": (name, stream, "application/octet-stream")},
                    )
                token = str((body.get("data") or {}).get("file_token") or "")
                if not token:
                    raise FeishuError("上传成功响应中缺少 file_token")
                return token
            except (FeishuError, httpx.HTTPError) as exc:
                last_error = exc
                retryable = not isinstance(exc, FeishuError) or (
                    exc.code in RETRYABLE_CODES or exc.status_code in {429, 500, 502, 503, 504}
                )
                if not retryable or attempt == 3:
                    raise
                self.sleep(min(10, 2 ** attempt))
        raise FeishuError(f"文件上传失败：{last_error}")

    def _upload_chunked(self, path: Path, name: str, size: int) -> str:
        prepared = self.request(
            "POST",
            "/drive/v1/files/upload_prepare",
            json={
                "file_name": name,
                "parent_type": "explorer",
                "parent_node": "",
                "size": size,
            },
        ).get("data") or {}
        upload_id = str(prepared.get("upload_id") or "")
        block_size = int(prepared.get("block_size") or 4 * 1024 * 1024)
        block_num = int(prepared.get("block_num") or math.ceil(size / block_size))
        if not upload_id:
            raise FeishuError("分片预上传未返回 upload_id")
        with path.open("rb") as stream:
            for seq in range(block_num):
                chunk = stream.read(block_size)
                self.request(
                    "POST",
                    "/drive/v1/files/upload_part",
                    data={
                        "upload_id": upload_id,
                        "seq": str(seq),
                        "size": str(len(chunk)),
                    },
                    files={"file": ("part", chunk, "application/octet-stream")},
                    retry_safe=True,
                )
        body = self.request(
            "POST",
            "/drive/v1/files/upload_finish",
            json={"upload_id": upload_id, "block_num": block_num},
            retry_safe=True,
        )
        token = str((body.get("data") or {}).get("file_token") or "")
        if not token:
            raise FeishuError("分片上传完成后未返回 file_token")
        return token

    def mount_file(
        self,
        space_id: str,
        file_token: str,
        parent_wiki_token: str,
        poll_timeout: float = 180,
    ) -> str:
        body = self.request(
            "POST",
            f"/wiki/v2/spaces/{space_id}/nodes/move_docs_to_wiki",
            json={
                "obj_type": "file",
                "obj_token": file_token,
                "parent_wiki_token": parent_wiki_token,
                "apply": False,
            },
        )
        data = body.get("data") or {}
        wiki_token = str(data.get("wiki_token") or "")
        if wiki_token:
            return wiki_token
        task_id = str(data.get("task_id") or "")
        if not task_id:
            raise FeishuError("挂载响应中缺少 wiki_token 和 task_id")
        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            status = self.request(
                "GET",
                f"/wiki/v2/tasks/{task_id}",
                params={"task_type": "move"},
                retry_safe=True,
            )
            task = ((status.get("data") or {}).get("task") or {})
            results = task.get("move_result") or []
            if results:
                first = results[0]
                node = first.get("node") or {}
                wiki_token = str(node.get("wiki_token") or node.get("node_token") or "")
                if wiki_token:
                    return wiki_token
                result_status = first.get("status")
                if result_status not in (None, 0):
                    raise FeishuError(
                        "挂载任务失败："
                        + str(first.get("status_msg") or result_status)
                    )
            self.sleep(2)
        raise FeishuError("等待知识库挂载结果超时，可稍后直接重试")

