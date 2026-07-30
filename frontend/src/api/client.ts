import type {
  AppSettingsInput,
  AuthStatus,
  MigrationPlan,
  Project,
  ProjectDraft,
  RunItem,
  RunSummary,
  ScanResult,
  SchedulePayload,
  TreeNode,
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
  try {
    response = await fetch(path, { ...init, headers, credentials: "same-origin" });
  } catch {
    throw new ApiError("无法连接本机迁移服务，请确认 Folder2Feishu 正在运行。");
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
  upload_calls_limit: 9_500,
  wiki_calls_minute: 0,
  wiki_calls_limit: 90
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
    rawStatus === "COMPLETE" || rawStatus === "DONE" ? "COMPLETED" : rawStatus
  ) as ScanResult["status"];
  return {
    scan_id: raw.scan_id ?? raw.run_id ?? "",
    status,
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
        ? "IDLE"
        : rawStatus === "QUOTA_PAUSED"
          ? "PAUSED"
          : rawStatus
  ) as RunSummary["state"];
  const quota = raw.quota ?? {};
  return {
    id: raw.id ?? raw.run_id ?? "",
    project_id: raw.project_id ?? "",
    state,
    started_at: raw.started_at,
    finished_at: raw.finished_at,
    current_path:
      raw.current_path ??
      (typeof current === "string" ? current : current?.relative_path ?? current?.path),
    total: raw.total ?? progress.total ?? 0,
    completed: raw.completed ?? progress.completed ?? progress.done ?? 0,
    failed: raw.failed ?? progress.failed ?? raw.errors?.length ?? 0,
    conflicts: raw.conflicts ?? progress.conflicts ?? 0,
    bytes_total: raw.bytes_total ?? progress.bytes_total ?? 0,
    bytes_completed: raw.bytes_completed ?? progress.bytes_completed ?? 0,
    eta_seconds: raw.eta_seconds ?? raw.eta,
    quota: {
      ...emptyQuota,
      ...quota,
      upload_calls_used: quota.upload_calls_used ?? quota.used ?? 0,
      upload_calls_limit: quota.upload_calls_limit ?? quota.budget ?? 9_500,
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
      upload_qps: Number(value.upload_qps ?? 4),
      wiki_calls_per_minute: Number(value.wiki_calls_per_minute ?? 90),
      daily_upload_budget: Number(value.daily_upload_budget ?? 9_500)
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
      upload_qps: Number(saved.upload_qps ?? 4),
      wiki_calls_per_minute: Number(saved.wiki_calls_per_minute ?? 90),
      daily_upload_budget: Number(saved.daily_upload_budget ?? 9_500)
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
  getTree: (id: string) => request<TreeNode[]>(`${API_ROOT}/projects/${id}/tree`),
  buildPlan: (id: string, confirm = false) =>
    request<Record<string, any>>(`${API_ROOT}/projects/${id}/plan`, {
      method: "POST",
      ...body({ confirmed: confirm })
    }).then(normalizePlan),
  getPlan: (id: string) =>
    request<Record<string, any>>(`${API_ROOT}/projects/${id}/plan`).then(normalizePlan),
  startRun: (id: string) =>
    request<{ run_id: string }>(`${API_ROOT}/projects/${id}/runs`, {
      method: "POST",
      ...body({})
    }),
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
    ).then((value) => ({
      matched: value.matched ?? 0,
      conflicts: Array.isArray(value.conflicts) ? value.conflicts.length : value.conflicts ?? 0,
      missing: value.missing ?? value.missing_remote ?? 0,
      checked_at: value.checked_at ?? new Date().toISOString()
    })),
  getAudit: (projectId: string) =>
    request<{ events?: unknown[]; items?: RunItem[] }>(
      `${API_ROOT}/projects/${projectId}/audit`
    ),
  exportAudit: (projectId: string, format: "csv" | "json") =>
    blobRequest(`${API_ROOT}/projects/${projectId}/audit?format=${format}`),
  getSchedule: (projectId: string) =>
    request<SchedulePayload>(`${API_ROOT}/projects/${projectId}/schedule`),
  saveSchedule: (projectId: string, value: SchedulePayload) =>
    request<SchedulePayload>(`${API_ROOT}/projects/${projectId}/schedule`, {
      method: "PUT",
      ...body(value)
    })
};
