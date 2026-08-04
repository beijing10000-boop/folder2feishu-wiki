"""Application service container shared by the HTTP UI and headless CLI."""

from __future__ import annotations

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
    parse_drive_folder_token,
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
    verify_drive_target,
    verify_oauth_identity,
    verify_source_root,
)

DRIVE_MAX_CHILDREN = 1_500
DRIVE_MAX_DEPTH = 15
DRIVE_MAX_TREE_NODES = 400_000


@dataclass(frozen=True, slots=True)
class PreflightReport:
    ready: bool
    writable: bool
    checked_at: str
    checks: list[dict[str, Any]]


class QuotaCapacityUnknown(FeishuError):
    """The tenant is not over quota, but no comparable CCM limit was returned."""


PLAN_PREVIEW_PER_KIND = 200


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
        drive, _ = self.feishu_services()
        return verify_drive_target(
            target_wiki_url,
            token_provider=self.token_provider(),
            drive=drive,
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
                drive_limiter = IntervalRateLimiter(settings.upload_qps, 1)
                wiki_limiter = IntervalRateLimiter(
                    settings.wiki_calls_per_minute,
                    60,
                )
                limits = RateLimitSet(
                    # Upload, multipart calls and Drive metadata writes share
                    # one conservative application bucket.  In particular,
                    # restoring a staging file title must not burst outside
                    # the upload rate after several workers finish together.
                    drive_upload=drive_limiter,
                    drive_write=drive_limiter,
                    # Feishu may enforce a tenant/application window across
                    # Wiki operations.  A shared bucket is slower than three
                    # independent 100/min buckets but prevents aggregate
                    # create/move/poll traffic from exceeding that window.
                    wiki=wiki_limiter,
                    wiki_create=wiki_limiter,
                    wiki_read=wiki_limiter,
                    wiki_write=wiki_limiter,
                )
                self._client = FeishuAPIClient(self.token_provider(), rate_limits=limits)
                self._api_client_key = key
            return DriveService(self._client), WikiService(self._client)

    def executor(self) -> MigrationExecutor:
        drive, wiki = self.feishu_services()
        settings = self.settings_store.load()
        quota = DailyQuotaStore(self.paths.quota, budget=settings.daily_upload_budget)
        return MigrationExecutor(self.store, drive, wiki, quota, drive_direct=True)

    def reconciler(self) -> RemoteReconciler:
        drive, wiki = self.feishu_services()
        return RemoteReconciler(self.store, drive, wiki, drive_direct=True)

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
                drive, _ = self.feishu_services()
                target_parent_token = parse_drive_folder_token(project.target_wiki_url)
                target_children = drive.list_folder(target_parent_token)
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
                local_depth = int(inventory["max_depth"])
                projected_depth = 1 + local_depth
                max_siblings = int(inventory["max_siblings"])
                total_nodes = int(inventory["files"]) + int(inventory["folders"])
                wrapper_matches = [
                    child
                    for child in target_children
                    if str(child.get("name") or "") == project.wrapper_name
                    and str(child.get("type") or "") == "folder"
                ]
                if len(wrapper_matches) > 1:
                    raise FeishuError("目标文件夹下存在多个同名根目录，请先人工消歧")
                projected_children = len(target_children) + (0 if wrapper_matches else 1)
                self.store.update_project(
                    project.id,
                    target_space_id="drive",
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
                    "目标云盘文件夹可访问",
                    f"已读取目标文件夹，当前包含 {len(target_children)} 个对象",
                    ok=True,
                )
                add(
                    "drive_depth",
                    "云盘目录深度可容纳",
                    (
                        f"本次新增目录最深 {projected_depth} 层；云盘完整路径上限为 {DRIVE_MAX_DEPTH} 层"
                        if projected_depth <= DRIVE_MAX_DEPTH
                        else f"本地目录将新增 {projected_depth} 层，超过云盘 {DRIVE_MAX_DEPTH} 层上限"
                    ),
                    ok=projected_depth <= DRIVE_MAX_DEPTH,
                )
                add(
                    "drive_children",
                    "云盘单层对象容量",
                    (
                        f"目标层预计 {projected_children} / {DRIVE_MAX_CHILDREN}；"
                        f"本地最大单层 {max_siblings} / {DRIVE_MAX_CHILDREN}"
                    ),
                    ok=(
                        projected_children <= DRIVE_MAX_CHILDREN
                        and max_siblings <= DRIVE_MAX_CHILDREN
                    ),
                )
                add(
                    "drive_tree_nodes",
                    "云盘树对象总量",
                    f"本地计划写入 {total_nodes} / {DRIVE_MAX_TREE_NODES} 个对象",
                    ok=total_nodes <= DRIVE_MAX_TREE_NODES,
                )
                add(
                    "target_edit",
                    "目标文件夹写入权限",
                    "读取验证已通过；首次小批迁移创建同名根目录时确认写入权限",
                    ok=True,
                    blocking=False,
                    warning=True,
                )
                try:
                    quota = drive.get_quota_detail(current_user_id)
                    quota_ok, quota_message = self._quota_capacity_check(
                        quota,
                        required_bytes=pending_upload_bytes,
                    )
                except QuotaCapacityUnknown as exc:
                    add(
                        "drive_quota",
                        "云盘容量检查",
                        str(exc),
                        ok=True,
                        blocking=False,
                        warning=True,
                    )
                except FeishuError as exc:
                    add(
                        "drive_quota",
                        "云盘容量检查",
                        str(exc),
                        ok=False,
                    )
                else:
                    add(
                        "drive_quota",
                        "云盘容量检查",
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
        summary = self.store.inventory_dashboard_summary(
            project_id,
            scan_id=project.current_scan_id,
        )
        scan_run_summary: dict[str, Any] = {}
        for run in self.store.list_job_runs(project_id, limit=20):
            if run.scan_id == project.current_scan_id:
                scan_run_summary = dict(run.summary or {})
                break
        return {
            **summary,
            "hashes_computed": int(scan_run_summary.get("hashes_computed", 0)),
            "hashes_reused": int(scan_run_summary.get("hashes_reused", 0)),
            "hash_workers": int(scan_run_summary.get("hash_workers", 0)),
            "elapsed_seconds": float(scan_run_summary.get("elapsed_seconds", 0)),
            "items_per_second": float(scan_run_summary.get("items_per_second", 0)),
            "megabytes_per_second": float(scan_run_summary.get("megabytes_per_second", 0)),
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
        plan_id = self.store.latest_plan_id(project_id)
        if not plan_id:
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
        dashboard = self.store.plan_dashboard(
            project_id,
            plan_id,
            preview_limit=PLAN_PREVIEW_PER_KIND,
        )
        first = dashboard["first"]
        rows: list[dict[str, Any]] = []
        for action, raw_size in dashboard["previews"]:
            kind = action.action_type.value
            size = int(raw_size or 0)
            blocking = action.state in {
                MigrationState.CONFLICT,
                MigrationState.MANUAL_ACTION,
            }
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
                    "blocking": blocking,
                    "status": action.state.value,
                }
            )
        counts = dashboard["counts"]
        total_actions = int(dashboard["total"])
        writable_actions = (
            total_actions
            - counts.get(ActionType.SKIP.value, 0)
            - counts.get(ActionType.REPORT_MISSING.value, 0)
        )
        blocking_conflicts = dashboard["states"].get(MigrationState.CONFLICT.value, 0) + dashboard[
            "states"
        ].get(MigrationState.MANUAL_ACTION.value, 0)
        return {
            "id": plan_id,
            "plan_id": plan_id,
            "created_at": first.created_at.isoformat(),
            "counts": counts,
            "actions": rows,
            "items": rows,
            "total_actions": total_actions,
            "writable_actions": writable_actions,
            "estimated_upload_calls": dashboard["upload_calls"],
            "estimated_days": 0,
            "confirmed": dashboard["unconfirmed"] == 0,
            "blocking_conflicts": blocking_conflicts,
            "preview_limit_per_kind": PLAN_PREVIEW_PER_KIND,
        }

    def confirm_latest_plan(self, project_id: str) -> dict[str, Any]:
        plan_id = self.store.latest_plan_id(project_id)
        if not plan_id:
            raise ValueError("尚未生成迁移计划")
        dashboard = self.store.plan_dashboard(project_id, plan_id, preview_limit=1)
        if dashboard["states"].get(MigrationState.CONFLICT.value, 0) or dashboard["states"].get(
            MigrationState.MANUAL_ACTION.value, 0
        ):
            raise ValueError("计划仍包含冲突或人工处理项")
        self.store.confirm_plan_actions(project_id, plan_id)
        self.store.append_audit(
            project_id,
            "plan.confirmed",
            "用户已最终确认当前差异计划",
            payload={"plan_id": plan_id},
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

        raw_unlimited = ccm.get("unlimited")
        try:
            unlimited = ApplicationServices._quota_bool(
                raw_unlimited,
                field="unlimited",
            )
        except FeishuError:
            unlimited = None
        used = ApplicationServices._quota_bytes(ccm.get("used"), field="used")
        if unlimited:
            return (
                True,
                f"云文档容量不限额；当前已用 {used} 字节，本地待迁移 {required_bytes} 字节",
            )

        raw_total = ccm.get("quota")
        try:
            total = ApplicationServices._quota_bytes(raw_total, field="quota")
        except FeishuError as exc:
            if unlimited is False:
                raise
            raise QuotaCapacityUnknown(
                "飞书确认租户当前未超限，但未返回可比较的云文档个人容量；"
                "此项不阻断迁移，实际上传仍受飞书服务端容量限制"
            ) from exc
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
            if normalized in {"true", "1", "yes", "unlimited"}:
                return True
            if normalized in {"false", "0", "no"}:
                return False
        raise FeishuError(f"飞书云文档容量的 {field} 字段无效")

    @staticmethod
    def _max_siblings(items: list[Any]) -> int:
        counts: dict[str | None, int] = {}
        for item in items:
            counts[item.parent_rel_path] = counts.get(item.parent_rel_path, 0) + 1
        return max(counts.values(), default=0)
