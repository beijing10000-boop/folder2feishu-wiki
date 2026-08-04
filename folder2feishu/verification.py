"""Read-only configuration verification used by the first-run desktop page."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .feishu import (
    DriveService,
    FeishuError,
    StoredUserTokenProvider,
    WikiService,
    parse_drive_folder_token,
    parse_wiki_token,
)
from .runtime import RuntimePaths, assert_runtime_outside_source
from .settings import DEFAULT_SCOPES

APP_CREDENTIAL_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    kind: str
    message: str
    details: dict[str, str | int | float | bool] = field(default_factory=dict)
    ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "message": self.message,
            "details": self.details,
        }


def verify_app_credentials(
    app_id: str,
    app_secret: str,
    *,
    client: httpx.Client | None = None,
) -> VerificationResult:
    """Ask Feishu to validate the app pair without retaining the tenant token."""

    app_id = app_id.strip()
    if not app_id or not app_secret:
        raise ValueError("请先保存飞书 App ID 和 App Secret")

    owned_client = client is None
    http = client or httpx.Client(
        timeout=httpx.Timeout(20, connect=10),
        follow_redirects=False,
    )
    response: httpx.Response | None = None
    payload: dict[str, Any] = {}
    try:
        response = http.post(
            APP_CREDENTIAL_URL,
            json={"app_id": app_id, "app_secret": app_secret},
            headers={"Accept": "application/json"},
        )
        try:
            decoded = response.json()
        except ValueError as exc:
            raise FeishuError("飞书应用凭据验证返回了无法识别的响应") from exc
        if not isinstance(decoded, dict):
            raise FeishuError("飞书应用凭据验证返回了无法识别的响应")
        payload = decoded
        code = int(payload.get("code") or 0)
        tenant_token_present = bool(str(payload.get("tenant_access_token") or ""))
        if response.status_code >= 400 or code != 0 or not tenant_token_present:
            suffix = f"（飞书错误码 {code}）" if code else ""
            raise FeishuError(f"飞书未接受当前 App ID / App Secret{suffix}")
        return VerificationResult(
            kind="app",
            message="飞书已确认 App ID 与 App Secret 有效；临时验证令牌已丢弃",
            details={"credential_valid": True},
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise FeishuError("无法连接飞书验证应用凭据，请检查网络后重试") from exc
    finally:
        # Never persist or return the tenant token. Clearing the decoded body also
        # shortens the lifetime of that sensitive value in this process.
        payload.clear()
        if response is not None:
            response.close()
        if owned_client:
            http.close()


def verify_oauth_identity(
    token_provider: StoredUserTokenProvider,
    drive: DriveService,
) -> VerificationResult:
    """Validate the fixed OAuth token, all required scopes and current user."""

    bundle = token_provider.load()
    if bundle is None:
        raise FeishuError("尚未完成飞书 OAuth 授权，请先点击“开始飞书授权”")
    missing = set(DEFAULT_SCOPES) - set(bundle.scopes)
    if missing:
        raise FeishuError("OAuth 授权缺少必需权限：" + "、".join(sorted(missing)))
    if not bundle.refresh_valid(now=time.time()):
        raise FeishuError("OAuth 刷新授权已失效，请重新点击“开始飞书授权”")

    # Calling the provider also performs refresh-token rotation when required.
    token_provider()
    profile = drive.get_current_user_info()
    user_id = str(profile.get("user_id") or "").strip()
    if not user_id:
        raise FeishuError("飞书未返回授权用户的 user_id，请确认五项权限已开通后重新授权")
    user_name = str(
        profile.get("name")
        or profile.get("en_name")
        or profile.get("display_name")
        or "当前飞书用户"
    ).strip()
    return VerificationResult(
        kind="oauth",
        message=f"OAuth 固定操作身份与五项权限已验证：{user_name}",
        details={
            "user_name": user_name,
            "scope_count": len(DEFAULT_SCOPES),
        },
    )


def verify_source_root(
    source_root: str,
    runtime_paths: RuntimePaths,
) -> VerificationResult:
    """Perform a shallow, read-only Windows directory check."""

    raw = source_root.strip()
    if not raw:
        raise ValueError("本地根目录不能为空")
    source = Path(raw).expanduser()
    if not source.is_absolute():
        raise ValueError("本地根目录必须是 Windows 绝对路径或 UNC 路径")
    try:
        if not source.exists():
            raise ValueError("本地根目录不存在")
        if not source.is_dir():
            raise ValueError("本地根目录不是文件夹")
        resolved = source.resolve(strict=True)
        assert_runtime_outside_source(runtime_paths, resolved)
        with os.scandir(resolved) as entries:
            next(entries, None)
    except PermissionError as exc:
        raise ValueError("当前 Windows 用户无权枚举本地根目录") from exc
    except OSError as exc:
        raise ValueError(f"无法读取本地根目录：{exc.strerror or exc}") from exc
    return VerificationResult(
        kind="source",
        message="本地根目录存在且根层可读取；应用运行数据位于源目录之外",
        details={"normalized_path": str(resolved)},
    )


def verify_wiki_target(
    target_wiki_url: str,
    *,
    token_provider: StoredUserTokenProvider,
    drive: DriveService,
    wiki: WikiService,
) -> VerificationResult:
    """Read a Wiki parent and its page-edit permission without a write probe."""

    # Give an actionable OAuth error before attempting Wiki calls.
    verify_oauth_identity(token_provider, drive)
    node_token = parse_wiki_token(target_wiki_url)
    node = wiki.get_node(node_token)
    space_id = str(node.get("space_id") or "").strip()
    resolved_node_token = str(node.get("node_token") or "").strip()
    if not space_id or not resolved_node_token:
        raise FeishuError("飞书知识库响应缺少 space_id 或 node_token")
    if not drive.can_edit_wiki(resolved_node_token):
        raise FeishuError("当前 OAuth 用户可以读取目标节点，但没有页面编辑权限")
    title = str(node.get("title") or node.get("obj_token") or "目标父节点").strip()
    return VerificationResult(
        kind="target",
        message=(
            f"已读取知识库目标“{title}”并确认页面编辑权限；容器编辑能力将在首个小批试迁时确认"
        ),
        details={
            "space_id": space_id,
            "node_token": resolved_node_token,
            "title": title,
            "page_editable": True,
            "container_edit_requires_pilot": True,
        },
    )


def verify_drive_target(
    target_drive_url: str,
    *,
    token_provider: StoredUserTokenProvider,
    drive: DriveService,
) -> VerificationResult:
    """Read the selected Drive folder without creating or deleting objects."""

    identity = verify_oauth_identity(token_provider, drive)
    folder_token = parse_drive_folder_token(target_drive_url)
    children = drive.list_folder(folder_token)
    return VerificationResult(
        kind="target",
        message=(
            f"已读取目标云盘文件夹，当前包含 {len(children)} 个对象；"
            "写入能力将在首个小批试迁创建根目录时确认"
        ),
        details={
            "folder_token": folder_token,
            "child_count": len(children),
            "user_id": identity.details.get("user_id", ""),
            "container_edit_requires_pilot": True,
        },
    )
