import type {
  AppSettingsInput,
  AuthStatus,
  MigrationPlan,
  Project,
  ProjectDraft,
  RuntimeLogEntry,
  RunItem,
  RunSummary,
  ScanResult,
  TreeNode,
  UploadProgress,
  VerificationResult
} from "../types";
import { mockRequest } from "./mock";

const API_ROOT = "/api/v2";
const DEMO = import.meta.env.VITE_DEMO === "1";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
let csrfToken: string | undefined;
let csrfPromise: Promise<string> | undefined;

export class ApiError extends Error {
  status: number;
  code?: string;
  details?: unknown;

  constructor(message: string, status = 0, code?: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function loadCsrfToken(): Promise<string> {
  if (csrfToken) return csrfToken;
  if (!csrfPromise) {
    csrfPromise = (async () => {
      if (DEMO) {
        const session = await mockRequest<{ csrf_token: string }>(`${API_ROOT}/session`);
        return session.csrf_token;
      }
      const response = await fetch(`${API_ROOT}/session`, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json", "Cache-Control": "no-store" }
      });
      if (!response.ok) throw new ApiError("无法建立安全会话，请刷新页面后重试。", response.status);
      const session = (await response.json()) as { csrf_token?: string };
      if (!session.csrf_token) throw new ApiError("服务未返回 CSRF 安全令牌。");
      return session.csrf_token;
    })();
  }
  csrfToken = await csrfPromise;
  return csrfToken;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (!SAFE_METHODS.has(method)) headers.set("X-F2F-CSRF", await loadCsrfToken());
  if (DEMO) return mockRequest<T>(path, init);

  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/json");

  let response: Response;
  const controller = new AbortController();
  const timeoutMs = SAFE_METHODS.has(method) ? 20_000 : 15_000;
  const timeout = window.setTimeout(() => controller.abort("timeout"), timeoutMs);
  const abortForwarder = () => controller.abort(init.signal?.reason);
  init.signal?.addEventListener("abort", abortForwarder, { once: true });
  try {
    response = await fetch(path, {
      ...init,
      headers,
      signal: controller.signal,
      credentials: "same-origin"
    });
  } catch (error) {
    if (controller.signal.aborted && !init.signal?.aborted) {
      throw new ApiError("本机服务响应超时，后台任务不会因此中断，请稍后刷新任务状态。");
    }
    throw new ApiError("无法连接本机迁移服务，请确认 Folder2Feishu 正在运行。");
  } finally {
    window.clearTimeout(timeout);
    init.signal?.removeEventListener("abort", abortForwarder);
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const error = payload?.error;
    throw new ApiError(
      error?.message ?? payload?.detail ?? `请求失败（HTTP ${response.status}）`,
      response.status,
      error?.code,
      error?.details
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

const body = (value: unknown): Pick<RequestInit, "body" | "headers"> => ({
  body: JSON.stringify(value),
  headers: { "Content-Type": "application/json" }
});

const emptyQuota = {
  upload_calls_used: 0,
  upload_calls_limit: 0,
  wiki_calls_minute: 0,
  wiki_calls_limit: 100
};

const severity = (value: string): "ok" | "warning" | "error" | "info" => {
  const normalized = String(value ?? "").toLowerCase();
  if (["ok", "pass", "passed", "ready", "success"].includes(normalized)) return "ok";
  if (["warning", "warn", "manual"].includes(normalized)) return "warning";
  if (["error", "fail", "failed", "blocked"].includes(normalized)) return "error";
  return "info";
};

const normalizeCheck = (item: Record<string, any>) => ({
  code: item.code ?? item.key ?? "CHECK",
  title: item.title ?? item.label ?? item.relative_path ?? "预检项目",
  message: item.message ?? "",
  severity: severity(item.severity ?? item.status),
  count: item.count,
  blocking: Boolean(item.blocking)
});

const normalizeScan = (raw: Record<string, any>): ScanResult => {
  const counts = raw.summary ?? raw.counts ?? {};
  const rawStatus = String(raw.status ?? raw.state ?? "").toUpperCase();
  const status = (
    rawStatus === "COMPLETE" || rawStatus === "DONE"
      ? "COMPLETED"
      : rawStatus === "QUEUED"
        ? "PENDING"
        : rawStatus
  ) as ScanResult["status"];
  return {
    scan_id: raw.scan_id ?? raw.run_id ?? "",
    run_id: raw.run_id,
    status,
    scanned_items: raw.scanned_items ?? 0,
    current_path: raw.current_path ?? "",
    stage: raw.stage,
    last_message: raw.last_message,
    heartbeat_at: raw.heartbeat_at,
    summary: {
      files: counts.files ?? counts.file ?? 0,
      folders: counts.folders ?? counts.directories ?? counts.folder ?? 0,
      bytes: counts.bytes ?? counts.total_bytes ?? 0,
      empty_files: counts.empty_files ?? counts.empty ?? counts.zero_byte ?? 0,
      placeholders: counts.placeholders ?? counts.offline ?? counts.onedrive_placeholders ?? 0,
      too_long_names: counts.too_long_names ?? counts.too_long ?? counts.long_names ?? 0,
      unreadable: counts.unreadable ?? 0,
      max_depth: counts.max_depth ?? 0,
      max_siblings: counts.max_siblings ?? 0,
      upload_calls: counts.upload_calls ?? raw.estimated_upload_calls ?? 0,
      hashes_computed: counts.hashes_computed ?? 0,
      hashes_reused: counts.hashes_reused ?? 0,
      estimated_days: counts.estimated_days ?? raw.estimated_days ?? 0,
      scan_complete: raw.complete ?? counts.scan_complete ?? status === "COMPLETED"
    },
    checks: (raw.checks ?? raw.issues ?? []).map(normalizeCheck),
    tree: raw.tree ?? []
  };
};

const normalizePlan = (raw: Record<string, any>): MigrationPlan => {
  const rawCounts = raw.counts ?? raw.actions ?? [];
  const normalizeKind = (value: string) => {
    const kind = String(value ?? "").toUpperCase();
    return kind === "REPORT_MISSING" ? "MISSING" : kind;
  };
  const counts = Array.isArray(rawCounts)
    ? rawCounts.map((item) => ({ ...item, kind: normalizeKind(item.kind ?? item.action) }))
    : Object.entries(rawCounts).map(([kind, count]) => ({ kind: normalizeKind(kind), count }));
  const actions = (raw.items ?? (Array.isArray(raw.actions) ? raw.actions : [])).map(
    (item: Record<string, any>) => ({
      ...item,
      kind: normalizeKind(item.kind ?? item.action),
      bytes: item.bytes ?? item.size ?? 0
    })
  );
  return {
    id: raw.id ?? raw.plan_id ?? "",
    created_at: raw.created_at ?? new Date().toISOString(),
    counts,
    total_actions:
      raw.total_actions ?? counts.reduce((sum: number, item: any) => sum + Number(item.count ?? 0), 0),
    writable_actions: raw.writable_actions ?? raw.total_actions ?? 0,
    estimated_upload_calls: raw.estimated_upload_calls ?? 0,
    estimated_days: raw.estimated_days ?? 0,
    confirmed: Boolean(raw.confirmed ?? raw.status === "confirmed"),
    actions
  } as MigrationPlan;
};

const normalizeRun = (raw: Record<string, any>): RunSummary => {
  const progress = typeof raw.progress === "object" ? raw.progress : {};
  const current = raw.current_item;
  const rawStatus = String(raw.state ?? raw.status ?? "IDLE").toUpperCase();
  const state = (
    rawStatus === "DONE"
      ? "COMPLETED"
      : rawStatus === "QUEUED"
        ? "RUNNING"
        : rawStatus === "QUOTA_PAUSED"
          ? "PAUSED"
          : rawStatus
  ) as RunSummary["state"];
  const quota = raw.quota ?? {};
  return {
    id: raw.id ?? raw.run_id ?? "",
    project_id: raw.project_id ?? "",
    kind: raw.kind,
    stage: raw.stage,
    state,
    started_at: raw.started_at,
    finished_at: raw.finished_at,
    current_path:
      raw.current_path ??
      (typeof current === "string" ? current : current?.relative_path ?? current?.path),
    last_message: raw.last_message,
    error: raw.error,
    heartbeat_at: raw.heartbeat_at,
    elapsed_seconds: raw.elapsed_seconds,
    retry_count: raw.retry_count ?? 0,
    worker_count: raw.worker_count ?? 0,
    in_flight: raw.in_flight ?? 0,
    skipped: raw.skipped ?? 0,
    total: raw.total ?? progress.total ?? 0,
    completed: raw.completed ?? progress.completed ?? progress.done ?? 0,
    failed: raw.failed ?? progress.failed ?? raw.errors?.length ?? 0,
    conflicts: raw.conflicts ?? progress.conflicts ?? 0,
    bytes_total: raw.bytes_total ?? progress.bytes_total ?? 0,
    bytes_completed: raw.bytes_completed ?? progress.bytes_completed ?? 0,
    ledger_bytes_completed: raw.ledger_bytes_completed,
    eta_seconds: raw.eta_seconds ?? raw.eta,
    eta_item_seconds: raw.eta_item_seconds,
    eta_bytes_seconds: raw.eta_bytes_seconds,
    eta_basis: raw.eta_basis,
    active_uploads: Array.isArray(raw.active_uploads)
      ? raw.active_uploads.map((upload: Record<string, any>) => ({
          action_id: String(upload.action_id ?? ""),
          relative_path: String(upload.relative_path ?? ""),
          status: String(upload.status ?? "UPLOADING") as UploadProgress["status"],
          completed_parts: Number(upload.completed_parts ?? 0),
          total_parts: Number(upload.total_parts ?? 0),
          uploaded_bytes: Number(upload.uploaded_bytes ?? 0),
          total_bytes: Number(upload.total_bytes ?? 0),
          percent: Number(upload.percent ?? 0),
          attempts: Number(upload.attempts ?? 0),
          last_error: upload.last_error ? String(upload.last_error) : undefined,
          updated_at: String(upload.updated_at ?? "")
        }))
      : [],
    quota: {
      ...emptyQuota,
      ...quota,
      upload_calls_used: quota.upload_calls_used ?? quota.used ?? 0,
      upload_calls_limit: quota.upload_calls_limit ?? quota.budget ?? 0,
      next_reset_at: quota.next_reset_at ?? quota.reset_at
    }
  };
};

async function blobRequest(path: string): Promise<Blob> {
  if (DEMO) return mockRequest<Blob>(path);
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "*/*" }
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new ApiError(payload?.error?.message ?? "审计文件导出失败", response.status);
  }
  return response.blob();
}

export const api = {
  isDemo: DEMO,
  getSession: () => loadCsrfToken().then(() => ({ ready: true })),
  health: () => request<{ ok: boolean; version: string }>(`${API_ROOT}/health`),
  getSettings: () =>
    request<Record<string, any>>(`${API_ROOT}/settings`).then((value) => ({
      app_id: value.app_id ?? "",
      redirect_uri: value.redirect_uri ?? "http://127.0.0.1:8000/oauth/callback",
      scopes: value.scopes ?? [],
      app_secret_configured: value.app_secret_configured ?? value.secret_configured ?? false,
      upload_qps: Number(value.upload_qps ?? 5),
      wiki_calls_per_minute: Number(value.wiki_calls_per_minute ?? 100),
      daily_upload_budget: Number(value.daily_upload_budget ?? 0)
    })),
  saveSettings: (value: AppSettingsInput) =>
    request<Record<string, any>>(`${API_ROOT}/settings`, {
      method: "PUT",
      ...body(value)
    }).then((saved) => ({
      app_id: saved.app_id ?? "",
      redirect_uri: saved.redirect_uri,
      scopes: saved.scopes ?? [],
      app_secret_configured: saved.app_secret_configured ?? saved.secret_configured ?? false,
      upload_qps: Number(saved.upload_qps ?? 5),
      wiki_calls_per_minute: Number(saved.wiki_calls_per_minute ?? 100),
      daily_upload_budget: Number(saved.daily_upload_budget ?? 0)
    })),
  getAuthStatus: () => request<AuthStatus>(`${API_ROOT}/auth/status`),
  startAuth: () =>
    request<{ authorization_url: string }>(`${API_ROOT}/auth/start`, {
      method: "POST",
      ...body({})
    }),
  verifyApp: () =>
    request<VerificationResult>(`${API_ROOT}/verify/app`, {
      method: "POST",
      ...body({})
    }),
  verifyOauth: () =>
    request<VerificationResult>(`${API_ROOT}/verify/oauth`, {
      method: "POST",
      ...body({})
    }),
  verifySource: (sourceRoot: string) =>
    request<VerificationResult>(`${API_ROOT}/verify/source`, {
      method: "POST",
      ...body({ source_root: sourceRoot })
    }),
  verifyTarget: (targetWikiUrl: string) =>
    request<VerificationResult>(`${API_ROOT}/verify/target`, {
      method: "POST",
      ...body({ target_wiki_url: targetWikiUrl })
    }),
  listProjects: () => request<Project[]>(`${API_ROOT}/projects`),
  createProject: (value: ProjectDraft) =>
    request<Project>(`${API_ROOT}/projects`, { method: "POST", ...body(value) }),
  getProject: (id: string) => request<Project>(`${API_ROOT}/projects/${id}`),
  getProjectTasks: (id: string) =>
    request<Record<string, any>[]>(`${API_ROOT}/projects/${id}/tasks`).then((items) =>
      items.map(normalizeRun)
    ),
  updateProject: (id: string, value: Partial<ProjectDraft>) =>
    request<Project>(`${API_ROOT}/projects/${id}`, { method: "PATCH", ...body(value) }),
  startScan: (id: string) =>
    request<{ run_id: string; status: string }>(`${API_ROOT}/projects/${id}/scan`, {
      method: "POST",
      ...body({})
    }),
  getScan: (id: string) =>
    request<Record<string, any>>(`${API_ROOT}/projects/${id}/scan`).then(normalizeScan),
  getPreflight: (id: string) =>
    request<Record<string, any>>(`${API_ROOT}/projects/${id}/preflight`).then((raw) => ({
      complete: raw.complete ?? Boolean(raw.ready),
      writable: raw.writable ?? Boolean(raw.ready),
      checked_at: raw.checked_at ?? new Date().toISOString(),
      checks: [...(raw.checks ?? []), ...(raw.issues ?? [])].map(normalizeCheck)
    })),
  getTree: async (id: string, parent?: string) => {
    const collected: TreeNode[] = [];
    let offset = 0;
    let hasMore = true;
    while (hasMore) {
      const query = new URLSearchParams({ offset: String(offset), limit: "500" });
      if (parent !== undefined) query.set("parent", parent);
      const page = await request<{
        items: TreeNode[];
        has_more: boolean;
      }>(`${API_ROOT}/projects/${id}/tree?${query}`);
      collected.push(...page.items);
      offset += page.items.length;
      hasMore = page.has_more && page.items.length > 0;
    }
    return collected;
  },
  startPlan: (id: string) =>
    request<Record<string, any>>(`${API_ROOT}/projects/${id}/plan`, {
      method: "POST",
      ...body({ confirmed: false })
    }).then(normalizeRun),
  confirmPlan: (id: string) =>
    request<Record<string, any>>(`${API_ROOT}/projects/${id}/plan`, {
      method: "POST",
      ...body({ confirmed: true })
    }).then(normalizePlan),
  buildPlan: (id: string, confirm = false) =>
    confirm
      ? api.confirmPlan(id)
      : api.startPlan(id),
  getPlan: (id: string) =>
    request<Record<string, any>>(`${API_ROOT}/projects/${id}/plan`).then(normalizePlan),
  startRun: (id: string) =>
    request<Record<string, any>>(`${API_ROOT}/projects/${id}/runs`, {
      method: "POST",
      ...body({})
    }).then(normalizeRun),
  getRun: (id: string) =>
    request<Record<string, any>>(`${API_ROOT}/runs/${id}`).then(normalizeRun),
  controlRun: (id: string, action: "pause" | "resume" | "stop" | "retry") =>
    request<Record<string, any>>(`${API_ROOT}/runs/${id}/${action}`, {
      method: "POST",
      ...body({})
    }).then(normalizeRun),
  reconcile: (projectId: string) =>
    request<Record<string, any>>(
      `${API_ROOT}/projects/${projectId}/reconcile`,
      { method: "POST", ...body({}) }
    ).then(normalizeRun),
  getAudit: (projectId: string, afterId?: number) =>
    request<{ events?: unknown[]; items?: RunItem[]; next_after_id?: number }>(
      `${API_ROOT}/projects/${projectId}/audit${afterId ? `?after_id=${afterId}` : ""}`
    ),
  getRuntimeLogs: (after?: number) =>
    request<{ entries: RuntimeLogEntry[]; next_after: number; reset: boolean }>(
      `${API_ROOT}/runtime/logs${after === undefined ? "" : `?after=${after}`}`
    ),
  exportAudit: (projectId: string, format: "csv" | "json") =>
    blobRequest(`${API_ROOT}/projects/${projectId}/audit?format=${format}`)
};
