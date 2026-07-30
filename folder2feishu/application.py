"""Application service container shared by the HTTP UI and headless CLI."""

from __future__ import annotations

import math
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .core import (
    ActionType,
    CoreStore,
    InventoryScanner,
    IssueSeverity,
    MigrationPlanner,
    MigrationState,
    Project,
)
from .executor import MigrationExecutor, RemoteReconciler
from .feishu import (
    DEFAULT_SCOPES,
    DriveService,
    FeishuAPIClient,
    FeishuError,
    IntervalRateLimiter,
    MissingScopesError,
    OAuthClient,
    OAuthError,
    RateLimitSet,
    StoredUserTokenProvider,
    WikiService,
    parse_wiki_token,
    save_app_secret,
    stored_app_secret_provider,
)
from .job_control import BackgroundJobManager
from .quota import DailyQuotaStore
from .runtime import RuntimePaths, assert_runtime_outside_source
from .security import CredentialStore, create_credential_store
from .settings import PublicSettings, SettingsStore
from .verification import (
    VerificationResult,
    verify_app_credentials,
    verify_oauth_identity,
    verify_source_root,
    verify_wiki_target,
)

WIKI_MAX_CHILDREN = 2_000


@dataclass(frozen=True, slots=True)
class PreflightReport:
    ready: bool
    writable: bool
    checked_at: str
    checks: list[dict[str, Any]]


class ApplicationServices:
    """Own process-wide stores without ever returning secrets to the browser."""

    def __init__(
        self,
        *,
        paths: RuntimePaths | None = None,
        credentials: CredentialStore | None = None,
        verification_client: httpx.Client | None = None,
    ) -> None:
        self.paths = (paths or RuntimePaths.discover()).ensure()
        self.settings_store = SettingsStore(self.paths)
        self.credentials = credentials or create_credential_store(self.paths.credentials)
        self.store = CoreStore(self.paths.database)
        self.scanner = InventoryScanner(self.store)
        self.planner = MigrationPlanner(self.store)
        self.jobs = BackgroundJobManager(max_workers=2)
        self._lock = threading.RLock()
        self._client: FeishuAPIClient | None = None
        self._token_provider: StoredUserTokenProvider | None = None
        self._client_settings_key = ""
        self._api_client_key = ""
        self._verification_client = verification_client

    def close(self) -> None:
        self.jobs.close()
        with self._lock:
            if self._client:
                self._client.close()
                self._client = None
        self.store.close()

    def public_settings(self) -> dict[str, Any]:
        settings = self.settings_store.load()
        return {
            **asdict(settings),
            "secret_configured": bool(self.credentials.get("feishu.app_secret")),
            "app_secret_configured": bool(self.credentials.get("feishu.app_secret")),
        }

    def save_settings(
        self, settings: PublicSettings, *, app_secret: str | None = None
    ) -> dict[str, Any]:
        previous = self.settings_store.load()
        if previous.app_id and previous.app_id != settings.app_id:
            self.credentials.delete("feishu.user_token_bundle")
        self.settings_store.save(settings)
        if app_secret:
            save_app_secret(self.credentials, app_secret)
        self._reset_feishu_client()
        return self.public_settings()

    def auth_status(self) -> dict[str, Any]:
        settings = self.settings_store.load()
        configured = bool(settings.app_id and self.credentials.get("feishu.app_secret"))
        if not configured:
            return {
                "configured": False,
                "authorized": False,
                "scopes": [],
                "missing_scopes": sorted(DEFAULT_SCOPES),
                "message": "请先保存飞书 App ID 和 App Secret",
            }
        try:
            bundle = self.token_provider().load()
        except OAuthError as exc:
            return {
                "configured": True,
                "authorized": False,
                "scopes": [],
                "missing_scopes": sorted(DEFAULT_SCOPES),
                "message": str(exc),
            }
        if bundle is None:
            return {
                "configured": True,
                "authorized": False,
                "scopes": [],
                "missing_scopes": sorted(DEFAULT_SCOPES),
                "message": "尚未完成飞书 OAuth 授权",
            }
        missing = sorted(set(DEFAULT_SCOPES) - set(bundle.scopes))
        return {
            "configured": True,
            "authorized": not missing and bundle.refresh_valid(now=datetime.now(UTC).timestamp()),
            "expires_at": datetime.fromtimestamp(bundle.expires_at, UTC).isoformat(),
            "scopes": sorted(bundle.scopes),
            "missing_scopes": missing,
            "message": "授权可用" if not missing else "授权缺少必需权限",
        }

    def verify_app_configuration(self) -> VerificationResult:
        settings = self.settings_store.load()
        secret = self.credentials.get("feishu.app_secret") or ""
        return verify_app_credentials(
            settings.app_id,
            secret,
            client=self._verification_client,
        )

    def verify_oauth_configuration(self) -> VerificationResult:
        settings = self.settings_store.load()
        if not settings.app_id or not self.credentials.get("feishu.app_secret"):
            raise FeishuError("请先保存并验证飞书 App ID 和 App Secret")
        drive, _ = self.feishu_services()
        return verify_oauth_identity(self.token_provider(), drive)

    def verify_source_configuration(self, source_root: str) -> VerificationResult:
        return verify_source_root(source_root, self.paths)

    def verify_target_configuration(self, target_wiki_url: str) -> VerificationResult:
        settings = self.settings_store.load()
        if not settings.app_id or not self.credentials.get("feishu.app_secret"):
            raise FeishuError("请先保存并验证飞书应用，再完成 OAuth 授权")
        if self.token_provider().load() is None:
            raise FeishuError("尚未完成飞书 OAuth 授权，请先点击“开始飞书授权”")
        drive, wiki = self.feishu_services()
        return verify_wiki_target(
            target_wiki_url,
            token_provider=self.token_provider(),
            drive=drive,
            wiki=wiki,
        )

    def token_provider(self) -> StoredUserTokenProvider:
        settings = self.settings_store.load()
        if not settings.app_id:
            raise OAuthError("Feishu App ID is not configured")
        key = f"{settings.app_id}|{settings.redirect_uri}|" + " ".join(sorted(settings.scopes))
        with self._lock:
            if self._token_provider is None or self._client_settings_key != key:
                oauth = OAuthClient(
                    client_id=settings.app_id,
                    client_secret_provider=stored_app_secret_provider(self.credentials),
                    required_scopes=settings.scopes,
                )
                self._token_provider = StoredUserTokenProvider(oauth, self.credentials)
                self._client_settings_key = key
            return self._token_provider

    def begin_authorization(self) -> str:
        settings = self.settings_store.load()
        authorization = self.token_provider().begin_authorization(
            settings.redirect_uri, scopes=settings.scopes
        )
        return authorization.authorization_url

    def complete_authorization(self, *, code: str, state: str) -> None:
        self.token_provider().complete_authorization(code=code, state=state)

    def feishu_services(self) -> tuple[DriveService, WikiService]:
        settings = self.settings_store.load()
        key = f"{settings.app_id}|{settings.upload_qps}|{settings.wiki_calls_per_minute}"
        with self._lock:
            if self._client is None or self._api_client_key != key:
                if self._client:
                    self._client.close()
                limits = RateLimitSet(
                    drive_upload=IntervalRateLimiter(settings.upload_qps, 1),
                    wiki=IntervalRateLimiter(settings.wiki_calls_per_minute, 60),
                )
                self._client = FeishuAPIClient(self.token_provider(), rate_limits=limits)
                self._api_client_key = key
            return DriveService(self._client), WikiService(self._client)

    def executor(self) -> MigrationExecutor:
        drive, wiki = self.feishu_services()
        settings = self.settings_store.load()
        quota = DailyQuotaStore(self.paths.quota, budget=settings.daily_upload_budget)
        return MigrationExecutor(self.store, drive, wiki, quota)

    def reconciler(self) -> RemoteReconciler:
        drive, wiki = self.feishu_services()
        return RemoteReconciler(self.store, drive, wiki)

    def create_project(
        self,
        *,
        name: str,
        source_root: str,
        target_wiki_url: str,
        wrapper_name: str | None = None,
    ) -> Project:
        assert_runtime_outside_source(self.paths, source_root)
        return self.store.create_project(
            name=name,
            source_root=source_root,
            target_wiki_url=target_wiki_url,
            wrapper_name=wrapper_name,
        )

    def preflight(self, project_id: str) -> PreflightReport:
        project = self.store.get_project(project_id)
        checks: list[dict[str, Any]] = []

        def add(
            code: str,
            title: str,
            message: str,
            *,
            ok: bool,
            blocking: bool = True,
            warning: bool = False,
        ) -> None:
            checks.append(
                {
                    "code": code,
                    "key": code,
                    "title": title,
                    "label": title,
                    "message": message,
                    "severity": "warning" if warning else "ok" if ok else "error",
                    "status": "warning" if warning else "ok" if ok else "error",
                    "blocking": bool(blocking and not ok),
                }
            )

        add(
            "scan_complete",
            "本地盘点完整",
            "盘点已完整落库" if project.scan_complete else "必须先完成一次无中断的本地扫描",
            ok=project.scan_complete,
        )
        blocking_issues = [
            issue
            for issue in self.store.list_issues(project.id, scan_id=project.current_scan_id)
            if issue.severity == IssueSeverity.BLOCKING
        ]
        add(
            "source_items",
            "OneDrive 与文件状态",
            ("未发现阻断项" if not blocking_issues else f"发现 {len(blocking_issues)} 个阻断项"),
            ok=not blocking_issues,
        )
        if not project.scan_complete:
            return PreflightReport(
                ready=False,
                writable=False,
                checked_at=datetime.now(UTC).isoformat(),
                checks=checks,
            )

        auth = self.auth_status()
        add(
            "oauth",
            "固定 OAuth 用户身份",
            auth["message"],
            ok=bool(auth["authorized"]),
        )
        if auth["authorized"]:
            try:
                drive, wiki = self.feishu_services()
                token = parse_wiki_token(project.target_wiki_url)
                node = wiki.get_node(token)
                space_id = str(node.get("space_id") or "")
                if not space_id:
                    raise FeishuError("目标节点响应缺少 space_id")
                target_parent_token = str(node.get("node_token") or token)
                user_info = drive.get_current_user_info()
                current_user_id = str(user_info.get("user_id") or "")
                if not current_user_id:
                    raise FeishuError(
                        "飞书未返回当前授权用户的 user_id；请开通 contact:user.employee_id:readonly"
                    )
                if project.identity_key and project.identity_key != current_user_id:
                    raise FeishuError(
                        "当前 OAuth 用户与项目首次预检绑定的用户不一致，为避免所有权切换已停止"
                    )
                inventory = self.inventory_summary(project.id)
                pending_upload_bytes = self.planner.estimate_pending_upload_bytes(project.id)
                target_depth = self._wiki_node_depth(wiki, node)
                local_depth = int(inventory["max_depth"])
                projected_depth = target_depth + 1 + local_depth
                target_children = wiki.list_children(space_id, target_parent_token)
                children_ok, children_message = self._wiki_child_capacity(
                    target_children,
                    wrapper_name=project.wrapper_name,
                )
                self.store.update_project(
                    project.id,
                    target_space_id=space_id,
                    target_parent_node_token=target_parent_token,
                    identity_key=current_user_id,
                )
                add(
                    "oauth_identity",
                    "OAuth 操作身份已锁定",
                    "当前授权用户与项目绑定身份一致",
                    ok=True,
                )
                add(
                    "target_read",
                    "目标知识库可访问",
                    f"已读取知识空间 {space_id}",
                    ok=True,
                )
                add(
                    "wiki_depth",
                    "目标知识库深度可容纳",
                    (
                        f"预计最深层级 {projected_depth} / 50"
                        if projected_depth <= 50
                        else f"目标父节点与本地目录合计将达到 {projected_depth} 层，超过 50"
                    ),
                    ok=projected_depth <= 50,
                )
                add(
                    "wiki_children",
                    "目标父节点子节点容量",
                    children_message,
                    ok=children_ok,
                )
                can_edit = drive.can_edit_wiki(token)
                add(
                    "target_edit",
                    "目标父节点页面编辑权限",
                    ("页面编辑权限检查已通过；容器编辑能力将在小批试迁首个节点创建时确认")
                    if can_edit
                    else "当前 OAuth 用户没有目标父节点页面编辑权限",
                    ok=can_edit,
                )
                drive.get_root_folder_token()
                add(
                    "drive_root",
                    "用户云盘中转可用",
                    "已取得当前 OAuth 用户的云盘根目录",
                    ok=True,
                )
                try:
                    quota = drive.get_quota_detail(current_user_id)
                    quota_ok, quota_message = self._quota_capacity_check(
                        quota,
                        required_bytes=pending_upload_bytes,
                    )
                except FeishuError as exc:
                    add(
                        "drive_quota",
                        "云盘容量充足",
                        str(exc),
                        ok=False,
                    )
                else:
                    add(
                        "drive_quota",
                        "云盘容量充足",
                        quota_message,
                        ok=quota_ok,
                    )
            except (FeishuError, ValueError, OAuthError, MissingScopesError) as exc:
                add(
                    "remote_preflight",
                    "飞书远端预检",
                    str(exc),
                    ok=False,
                )

        ready = not any(check["blocking"] for check in checks)
        return PreflightReport(
            ready=ready,
            writable=ready,
            checked_at=datetime.now(UTC).isoformat(),
            checks=checks,
        )

    def inventory_summary(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_project(project_id)
        items = self.store.list_inventory(project_id, present=True)
        issues = self.store.list_issues(project_id, scan_id=project.current_scan_id)
        scan_run_summary: dict[str, Any] = {}
        for run in self.store.list_job_runs(project_id, limit=20):
            if run.scan_id == project.current_scan_id:
                scan_run_summary = dict(run.summary or {})
                break
        files = [item for item in items if item.kind.value == "FILE"]
        folders = [item for item in items if item.kind.value == "FOLDER"]
        calls = sum(
            1
            if (item.size or 0) <= 20 * 1024 * 1024
            else 2 + math.ceil((item.size or 0) / (4 * 1024 * 1024))
            for item in files
            if (item.size or 0) > 0
        )
        return {
            "files": len(files),
            "folders": len(folders),
            "bytes": sum(item.size or 0 for item in files),
            "empty_files": sum((item.size or 0) == 0 for item in files),
            "placeholders": sum(
                item.is_offline or item.is_recall_on_open or item.is_recall_on_data_access
                for item in items
            ),
            "too_long_names": sum(issue.code.value == "NAME_TOO_LONG" for issue in issues),
            "unreadable": sum(
                issue.code.value in {"STAT_ERROR", "HASH_ERROR", "ENUMERATION_ERROR"}
                for issue in issues
            ),
            "max_depth": max((item.depth for item in items), default=0),
            "max_siblings": self._max_siblings(items),
            "upload_calls": calls,
            "hashes_computed": int(scan_run_summary.get("hashes_computed", 0)),
            "hashes_reused": int(scan_run_summary.get("hashes_reused", 0)),
            "estimated_days": 0,
            "scan_complete": project.scan_complete,
        }

    def inventory_tree(self, project_id: str) -> list[dict[str, Any]]:
        items = self.store.list_inventory(project_id, present=True)
        nodes: dict[str, dict[str, Any]] = {}
        roots: list[dict[str, Any]] = []
        for item in items:
            node = {
                "id": item.id,
                "name": item.name,
                "relative_path": item.rel_path,
                "kind": item.kind.value.lower(),
                "size": item.size or 0,
                "status": item.state.value,
                "children": [],
            }
            nodes[item.rel_path] = node
            parent = (
                item.rel_path.rsplit("/", 1)[0]
                if "/" in item.rel_path
                else ""
                if item.rel_path
                else None
            )
            if parent is None:
                roots.append(node)
            elif parent in nodes:
                nodes[parent]["children"].append(node)
            else:
                roots.append(node)
        return roots

    def plan_payload(self, project_id: str) -> dict[str, Any]:
        actions = self.store.list_plan_actions(project_id)
        if not actions:
            return {
                "id": "",
                "plan_id": "",
                "created_at": datetime.now(UTC).isoformat(),
                "counts": {},
                "actions": [],
                "items": [],
                "total_actions": 0,
                "writable_actions": 0,
                "estimated_upload_calls": 0,
                "estimated_days": 0,
                "confirmed": False,
            }
        inventory = {item.id: item for item in self.store.list_inventory(project_id)}
        counts: dict[str, int] = {}
        rows: list[dict[str, Any]] = []
        upload_calls = 0
        for action in actions:
            kind = action.action_type.value
            counts[kind] = counts.get(kind, 0) + 1
            item = inventory.get(action.inventory_item_id or "")
            size = item.size or 0 if item else 0
            if action.action_type in {
                ActionType.UPLOAD,
                ActionType.VERSION_UPDATE,
            }:
                upload_calls += (
                    0
                    if size == 0
                    else 1
                    if size <= 20 * 1024 * 1024
                    else 2 + math.ceil(size / (4 * 1024 * 1024))
                )
            rows.append(
                {
                    "id": action.id,
                    "kind": kind,
                    "action": kind.lower(),
                    "relative_path": action.source_rel_path or action.previous_rel_path,
                    "previous_path": action.previous_rel_path or None,
                    "reason": action.reason,
                    "bytes": size,
                    "size": size,
                    "blocking": action.state
                    in {
                        MigrationState.CONFLICT,
                        MigrationState.MANUAL_ACTION,
                    },
                    "status": action.state.value,
                }
            )
        confirmed = all(
            bool((action.details or {}).get("plan_confirmed"))
            for action in actions
            if action.action_type not in {ActionType.SKIP, ActionType.REPORT_MISSING}
        )
        return {
            "id": actions[0].plan_id,
            "plan_id": actions[0].plan_id,
            "created_at": actions[0].created_at.isoformat(),
            "counts": counts,
            "actions": rows,
            "items": rows,
            "total_actions": len(actions),
            "writable_actions": sum(
                action.action_type not in {ActionType.SKIP, ActionType.REPORT_MISSING}
                for action in actions
            ),
            "estimated_upload_calls": upload_calls,
            "estimated_days": 0,
            "confirmed": confirmed,
            "blocking_conflicts": sum(row["blocking"] for row in rows),
        }

    def confirm_latest_plan(self, project_id: str) -> dict[str, Any]:
        actions = self.store.list_plan_actions(project_id)
        if not actions:
            raise ValueError("尚未生成迁移计划")
        if any(
            action.state in {MigrationState.CONFLICT, MigrationState.MANUAL_ACTION}
            for action in actions
        ):
            raise ValueError("计划仍包含冲突或人工处理项")
        for action in actions:
            self.store.update_plan_action(action.id, merge_details={"plan_confirmed": True})
        self.store.append_audit(
            project_id,
            "plan.confirmed",
            "用户已最终确认当前差异计划",
            payload={"plan_id": actions[0].plan_id},
        )
        return self.plan_payload(project_id)

    def _reset_feishu_client(self) -> None:
        with self._lock:
            if self._client:
                self._client.close()
            self._client = None
            self._token_provider = None
            self._client_settings_key = ""
            self._api_client_key = ""

    @staticmethod
    def _quota_capacity_check(
        quota: dict[str, Any],
        *,
        required_bytes: int,
    ) -> tuple[bool, str]:
        if required_bytes < 0:
            raise ValueError("required_bytes must not be negative")
        tenant_exceeded = quota.get("is_tenant_quota_exceeded")
        if not isinstance(tenant_exceeded, bool):
            raise FeishuError("飞书容量响应缺少有效的租户超额状态")
        if tenant_exceeded:
            return False, "飞书租户容量已超限，无法开始迁移"

        # The current Drive v2 schema names this list ``biz_infos``. Accept the
        # older/alternate ``biz_lists`` spelling defensively, but never treat a
        # missing or malformed list as sufficient capacity.
        businesses = quota.get("biz_infos")
        if businesses is None:
            businesses = quota.get("biz_lists")
        if not isinstance(businesses, list):
            raise FeishuError("飞书容量响应缺少有效的业务容量列表")
        ccm = next(
            (
                item
                for item in businesses
                if isinstance(item, dict) and str(item.get("name") or "").casefold() == "ccm"
            ),
            None,
        )
        if ccm is None:
            raise FeishuError("飞书容量响应缺少云文档容量（ccm）")

        unlimited = ApplicationServices._quota_bool(
            ccm.get("unlimited"),
            field="unlimited",
        )
        used = ApplicationServices._quota_bytes(ccm.get("used"), field="used")
        if unlimited:
            return (
                True,
                f"云文档容量不限额；当前已用 {used} 字节，本地待迁移 {required_bytes} 字节",
            )

        total = ApplicationServices._quota_bytes(ccm.get("quota"), field="quota")
        if used > total:
            return (
                False,
                f"云文档容量已超限：已用 {used} / 总量 {total} 字节",
            )
        available = max(0, total - used)
        if required_bytes > available:
            return (
                False,
                "云文档可用容量不足："
                f"可用 {available} 字节，本地待迁移 {required_bytes} 字节"
                f"（已用 {used} / 总量 {total}）",
            )
        return (
            True,
            "云文档容量充足："
            f"可用 {available} 字节，本地待迁移 {required_bytes} 字节"
            f"（已用 {used} / 总量 {total}）",
        )

    @staticmethod
    def _quota_bytes(value: Any, *, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, str | int):
            raise FeishuError(f"飞书云文档容量的 {field} 字段无效")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise FeishuError(f"飞书云文档容量的 {field} 字段无效") from exc
        if parsed < 0:
            raise FeishuError(f"飞书云文档容量的 {field} 字段无效")
        return parsed

    @staticmethod
    def _quota_bool(value: Any, *, field: str) -> bool:
        """Normalize boolean values emitted by different Drive v2 gateways."""

        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"true", "1"}:
                return True
            if normalized in {"false", "0"}:
                return False
        raise FeishuError(f"飞书云文档容量的 {field} 字段无效")

    @staticmethod
    def _wiki_node_depth(wiki: WikiService, node: dict[str, Any]) -> int:
        depth = 1
        current = node
        seen: set[str] = set()
        while True:
            parent = str(current.get("parent_node_token") or "")
            if not parent or parent == "0":
                return depth
            if parent in seen:
                raise FeishuError("目标知识库父节点链存在循环，无法安全计算层级")
            seen.add(parent)
            depth += 1
            if depth > 50:
                return depth
            current = wiki.get_node(parent)

    @staticmethod
    def _wiki_child_capacity(
        children: list[dict[str, Any]],
        *,
        wrapper_name: str,
    ) -> tuple[bool, str]:
        wrapper_matches = [
            child
            for child in children
            if str(child.get("title") or "") == wrapper_name
            and str(child.get("obj_type") or "") == "docx"
        ]
        if len(wrapper_matches) > 1:
            return (
                False,
                f"目标父节点下存在 {len(wrapper_matches)} 个同名 Docx 包装节点，必须先人工消歧",
            )
        additional = 0 if wrapper_matches else 1
        projected = len(children) + additional
        if projected > WIKI_MAX_CHILDREN:
            return (
                False,
                f"目标父节点已有 {len(children)} 个子节点，"
                f"新增根目录包装节点后将达到 {projected}，超过 {WIKI_MAX_CHILDREN}",
            )
        suffix = "（同名 Docx 包装节点已存在）" if wrapper_matches else ""
        return (
            True,
            f"预计迁移开始时为 {projected} / {WIKI_MAX_CHILDREN} 个子节点{suffix}",
        )

    @staticmethod
    def _max_siblings(items: list[Any]) -> int:
        counts: dict[str | None, int] = {}
        for item in items:
            counts[item.parent_rel_path] = counts.get(item.parent_rel_path, 0) + 1
        return max(counts.values(), default=0)
