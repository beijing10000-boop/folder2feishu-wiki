import type {
  AppSettings,
  AuditEvent,
  AuthStatus,
  MigrationPlan,
  PreflightResult,
  Project,
  RunItem,
  RuntimeLogEntry,
  RunSummary,
  ScanResult,
  TreeNode
} from "../types";

const now = () => new Date().toISOString();
const wait = (ms = 180) => new Promise((resolve) => setTimeout(resolve, ms));

const project: Project = {
  id: "prj_fabdazzle",
  name: "FabDazzle 文档迁移",
  source_root: "D:\\TechStyle\\Team FabDazzle - 文档",
  target_wiki_url:
    "https://pg6xd0yqgm.feishu.cn/drive/folder/DriveFolderTokenDemo",
  target_space_id: "drive",
  target_parent_token: "DriveFolderTokenDemo",
  wrapper_name: "Team FabDazzle - 文档",
  create_wrapper: true,
  mode: "safe_incremental",
  last_run_id: "run_20260730_1024",
  created_at: now(),
  updated_at: now()
};

let settings: AppSettings = {
  app_id: "cli_a8••••••b21",
  redirect_uri: "http://127.0.0.1:8000/oauth/callback",
  scopes: [
    "offline_access",
    "drive:drive",
    "drive:file:upload",
    "drive:quota_detail:read_one",
    "contact:user.employee_id:readonly"
  ],
  app_secret_configured: true,
  upload_qps: 5,
  wiki_calls_per_minute: 100,
  daily_upload_budget: 0
};

const auth: AuthStatus = {
  configured: true,
  authorized: true,
  app_id_masked: "cli_a8••••••b21",
  user_name: "Mack Luo",
  scopes: settings.scopes,
  expires_at: new Date(Date.now() + 70 * 60 * 1000).toISOString()
};

const tree: TreeNode[] = [
  {
    id: "root",
    name: "Team FabDazzle - 文档",
    relative_path: ".",
    kind: "folder",
    children: [
      {
        id: "apparel",
        name: "Apparel",
        relative_path: "Apparel",
        kind: "folder",
        children: [
          {
            id: "forecast",
            name: "Forecast",
            relative_path: "Apparel\\Forecast",
            kind: "folder",
            children: [
              {
                id: "forecast-file",
                name: "FW26 Demand Plan.xlsx",
                relative_path: "Apparel\\Forecast\\FW26 Demand Plan.xlsx",
                kind: "file",
                size: 3_482_113,
                status: "PLANNED"
              }
            ]
          },
          {
            id: "line-sheet",
            name: "Line Sheet - July.xlsx",
            relative_path: "Apparel\\Line Sheet - July.xlsx",
            kind: "file",
            size: 1_281_019,
            status: "PLANNED"
          }
        ]
      },
      {
        id: "design",
        name: "Design",
        relative_path: "Design",
        kind: "folder",
        children: [
          {
            id: "mood",
            name: "Moodboards",
            relative_path: "Design\\Moodboards",
            kind: "folder",
            children: []
          },
          {
            id: "deck",
            name: "Brand Review 2026.pptx",
            relative_path: "Design\\Brand Review 2026.pptx",
            kind: "file",
            size: 28_411_303,
            status: "PLANNED"
          }
        ]
      },
      {
        id: "zero",
        name: "旧资料索引.txt",
        relative_path: "旧资料索引.txt",
        kind: "file",
        size: 0,
        status: "DISCOVERED"
      }
    ]
  }
];

const checks = [
  {
    code: "FEISHU_PERMISSION",
    title: "云盘写入权限",
    message: "授权用户可访问目标云盘文件夹",
    severity: "ok" as const,
    blocking: false
  },
  {
    code: "DRIVE_CAPACITY",
    title: "飞书云空间容量",
    message: "预计写入 184.6 GB，当前可用 1.2 TB",
    severity: "ok" as const,
    blocking: false
  },
  {
    code: "ONEDRIVE_PLACEHOLDER",
    title: "OneDrive 本地可用性",
    message: "本地源目录中的文件均已下载，可进行只读迁移盘点",
    severity: "ok" as const,
    count: 0,
    blocking: false
  },
  {
    code: "ZERO_BYTE",
    title: "0 字节文件",
    message: "飞书不支持空文件；3 个文件会记录在报告中并自动跳过",
    severity: "warning" as const,
    count: 3,
    blocking: false
  },
  {
    code: "NAME_LENGTH",
    title: "名称长度",
    message: "2 个名称超过飞书 250 字符限制",
    severity: "warning" as const,
    count: 2,
    blocking: false
  },
  {
    code: "TREE_LIMITS",
    title: "云盘目录层级",
    message: "最大 9 层，单层最多 346 个节点，均在限制内",
    severity: "ok" as const,
    blocking: false
  }
];

const scan: ScanResult = {
  scan_id: "scan_20260730_1018",
  status: "COMPLETED",
  summary: {
    files: 35_706,
    folders: 4_218,
    bytes: 198_212_829_184,
    empty_files: 3,
    placeholders: 0,
    too_long_names: 2,
    unreadable: 0,
    max_depth: 9,
    max_siblings: 346,
    upload_calls: 36_109,
    estimated_days: 0,
    scan_complete: true
  },
  checks,
  tree
};

const actions = [
  {
    id: "a1",
    kind: "CREATE_FOLDER" as const,
    relative_path: "Apparel\\Forecast",
    reason: "飞书云盘中不存在对应目录"
  },
  {
    id: "a2",
    kind: "UPLOAD" as const,
    relative_path: "Apparel\\Forecast\\FW26 Demand Plan.xlsx",
    reason: "首次迁移",
    bytes: 3_482_113
  },
  {
    id: "a3",
    kind: "VERSION_UPDATE" as const,
    relative_path: "Design\\Brand Review 2026.pptx",
    reason: "本地 SHA-256 与上次迁移不同",
    bytes: 28_411_303
  },
  {
    id: "a4",
    kind: "MOVE" as const,
    relative_path: "Reporting Updates\\Weekly Trade.xlsx",
    reason: "Windows File ID 未变，目录位置发生变化"
  },
  {
    id: "a5",
    kind: "MISSING" as const,
    relative_path: "Archive\\FY23 Plan.pdf",
    reason: "本地文件已缺失；仅报告，不删除飞书内容"
  },
  {
    id: "a6",
    kind: "CONFLICT" as const,
    relative_path: "Design\\Creative Brief.docx",
    reason: "飞书节点被人工移动，需要人工确认",
    blocking: true
  }
];

const plan: MigrationPlan = {
  id: "plan_20260730_1021",
  created_at: now(),
  counts: [
    { kind: "CREATE_FOLDER", count: 4_218 },
    { kind: "UPLOAD", count: 35_694 },
    { kind: "MOVE", count: 4 },
    { kind: "RENAME", count: 1 },
    { kind: "VERSION_UPDATE", count: 7 },
    { kind: "MISSING", count: 18 },
    { kind: "SKIP", count: 1_284 },
    { kind: "CONFLICT", count: 1 }
  ],
  total_actions: 41_227,
  writable_actions: 39_924,
  estimated_upload_calls: 36_109,
  estimated_days: 0,
  confirmed: true,
  actions
};

let runStartedAt = Date.now() - 4_200_000;
let runState: RunSummary["state"] = "RUNNING";

const runItems: RunItem[] = [
  {
    id: "r1",
    relative_path: "Apparel\\Forecast\\FW26 Demand Plan.xlsx",
    status: "DONE",
    progress: 100,
    attempts: 1,
    updated_at: now()
  },
  {
    id: "r2",
    relative_path: "Design\\Brand Review 2026.pptx",
    status: "UPLOADING",
    progress: 62,
    attempts: 1,
    updated_at: now()
  },
  {
    id: "r3",
    relative_path: "Design\\Creative Brief.docx",
    status: "CONFLICT",
    progress: 0,
    attempts: 1,
    error_code: "REMOTE_CHANGED",
    error_message: "飞书节点已被人工移动",
    updated_at: now()
  },
  {
    id: "r4",
    relative_path: "Apparel\\blocked name?.xlsx",
    status: "RETRYABLE",
    progress: 0,
    attempts: 3,
    error_code: "1061045",
    error_message: "权限或父节点状态发生变化，等待重新对账",
    updated_at: now()
  }
];

const audit: AuditEvent[] = [
  {
    id: "e1",
    occurred_at: "2026-07-30T10:28:42+08:00",
    level: "SUCCESS",
    stage: "VERIFYING",
    relative_path: "Apparel\\Line Sheet - July.xlsx",
    message: "云盘对象回读成功",
    evidence: "object_token=file•••14d · SHA-256 matched"
  },
  {
    id: "e2",
    occurred_at: "2026-07-30T10:28:36+08:00",
    level: "INFO",
    stage: "WIKI_MOVING",
    relative_path: "Apparel\\Line Sheet - July.xlsx",
    message: "文件已直接写入目标云盘目录"
  },
  {
    id: "e3",
    occurred_at: "2026-07-30T10:27:51+08:00",
    level: "WARNING",
    stage: "RECONCILE",
    relative_path: "Design\\Creative Brief.docx",
    message: "检测到远端人工移动，已停止自动覆盖"
  }
];

const runtimeLogs: RuntimeLogEntry[] = [
  {
    id: "log-1",
    occurred_at: now(),
    level: "INFO",
    logger: "httpx",
    message: 'HTTP Request: POST https://open.feishu.cn/open-apis/drive/v1/files/upload_part "HTTP/1.1 200 OK"'
  },
  {
    id: "log-2",
    occurred_at: now(),
    level: "WARNING",
    logger: "folder2feishu.feishu.client",
    message: "Feishu rate limit reached; endpoint bucket deferred",
    path: "/drive/v1/files/upload_part",
    retry_count: 1
  }
];

const getRun = (): RunSummary => {
  const elapsed = runStartedAt ? (Date.now() - runStartedAt) / 1000 : 0;
  const baseCompleted = runState === "RUNNING" ? Math.min(28_441, 18_706 + Math.floor(elapsed * 2)) : 18_706;
  return {
    id: "run_20260730_1024",
    project_id: project.id,
    state: runState,
    started_at: runStartedAt ? new Date(runStartedAt).toISOString() : undefined,
    current_path:
      runState === "RUNNING" ? "Design\\Brand Review 2026.pptx · 分片 5/8" : undefined,
    total: 39_924,
    completed: baseCompleted,
    failed: 9,
    conflicts: 1,
    bytes_total: scan.summary.bytes,
    bytes_completed: Math.round(scan.summary.bytes * (baseCompleted / 39_924)),
    eta_seconds: runState === "RUNNING" ? 98_840 : undefined,
    worker_count: 6,
    in_flight: runState === "RUNNING" ? 6 : 0,
    active_uploads: runState === "RUNNING" ? [
      {
        action_id: "r2",
        relative_path: "Design\\Brand Review 2026.pptx",
        status: "UPLOADING",
        completed_parts: 5,
        total_parts: 8,
        uploaded_bytes: 5 * 4 * 1024 * 1024,
        total_bytes: 28_411_303,
        percent: 62.5,
        attempts: 6,
        updated_at: now()
      }
    ] : [],
    quota: {
      upload_calls_used: 8_742,
      upload_calls_limit: 0,
      wiki_calls_minute: 41,
      wiki_calls_limit: 100,
      next_reset_at: "2026-07-31T00:00:00+08:00"
    }
  };
};

const jsonBlob = (value: unknown, type: string) =>
  new Blob([typeof value === "string" ? value : JSON.stringify(value, null, 2)], { type });

export async function mockRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  await wait();
  const method = (init.method ?? "GET").toUpperCase();
  const body = init.body ? JSON.parse(String(init.body)) : {};
  const projectPath = `/api/v2/projects/${project.id}`;

  if (path === "/api/v2/session") return { csrf_token: "demo-csrf-token" } as T;
  if (path === "/api/v2/health") {
    return {
      ok: true,
      status: "ok",
      version: "3.0.0-rc.3-demo",
      database: "ok"
    } as T;
  }
  if (path === "/api/v2/settings" && method === "GET") return settings as T;
  if (path === "/api/v2/settings" && method === "PUT") {
    settings = {
      ...settings,
      ...body,
      app_secret_configured: Boolean(body.app_secret) || settings.app_secret_configured
    };
    return settings as T;
  }
  if (path === "/api/v2/auth/status") return auth as T;
  if (path === "/api/v2/auth/start" && method === "POST") {
    return { authorization_url: `${window.location.origin}/?oauth=demo-success` } as T;
  }
  if (path === "/api/v2/verify/app" && method === "POST") {
    return {
      ok: true,
      kind: "app",
      message: "飞书已确认 App ID 与 App Secret 有效；临时验证令牌已丢弃",
      details: { credential_valid: true }
    } as T;
  }
  if (path === "/api/v2/verify/oauth" && method === "POST") {
    return {
      ok: true,
      kind: "oauth",
      message: "用户授权固定操作身份与五项权限已验证：Mack Luo",
      details: { user_name: "Mack Luo", scope_count: 5 }
    } as T;
  }
  if (path === "/api/v2/verify/source" && method === "POST") {
    return {
      ok: true,
      kind: "source",
      message: "本地根目录存在且根层可读取；应用运行数据位于源目录之外",
      details: { normalized_path: body.source_root }
    } as T;
  }
  if (path === "/api/v2/verify/target" && method === "POST") {
    return {
      ok: true,
      kind: "target",
      message: "已读取云盘目标文件夹；实际写入能力将在首个小批试迁时确认",
      details: {
        folder_token: project.target_parent_token ?? "",
        child_count: 0,
        container_edit_requires_pilot: true
      }
    } as T;
  }
  if (path === "/api/v2/projects" && method === "GET") return [project] as T;
  if (path === "/api/v2/projects" && method === "POST") {
    Object.assign(project, body, { id: project.id, mode: "safe_incremental" });
    return project as T;
  }
  if (path === projectPath && method === "GET") return project as T;
  if (path === projectPath && method === "PATCH") {
    Object.assign(project, body, { updated_at: now() });
    return project as T;
  }
  if (path === `${projectPath}/scan` && method === "POST") {
    return { run_id: scan.scan_id, status: "RUNNING" } as T;
  }
  if (path === `${projectPath}/scan`) return scan as T;
  if (path === `${projectPath}/preflight`) {
    return {
      complete: true,
      writable: !checks.some((check) => check.blocking),
      checked_at: now(),
      checks
    } as PreflightResult as T;
  }
  if (path === `${projectPath}/tree`) return tree as T;
  if (path === `${projectPath}/plan` && method === "POST") {
    plan.confirmed = Boolean(body.confirmed ?? body.confirm);
    return plan as T;
  }
  if (path === `${projectPath}/plan`) return plan as T;
  if (path === `${projectPath}/runs` && method === "POST") {
    runStartedAt = Date.now();
    runState = "RUNNING";
    return { run_id: "run_20260730_1024" } as T;
  }
  if (path === "/api/v2/runs/run_20260730_1024") return getRun() as T;
  if (path.startsWith("/api/v2/runtime/logs")) {
    return { entries: runtimeLogs, next_after: 128, reset: false } as T;
  }
  if (path.endsWith("/pause")) runState = "PAUSED";
  if (path.endsWith("/resume")) {
    runState = "RUNNING";
    runStartedAt = Date.now();
  }
  if (path.endsWith("/stop")) runState = "STOPPED";
  if (path.endsWith("/retry")) runState = "RUNNING";
  if (path.includes("/api/v2/runs/") && method === "POST") return getRun() as T;
  if (path === `${projectPath}/reconcile` && method === "POST") {
    return { matched: 18_706, conflicts: 1, missing: 0, checked_at: now() } as T;
  }
  if (path.startsWith(`${projectPath}/audit`)) {
    if (path.includes("format=csv")) {
      const csv = [
        "occurred_at,level,stage,relative_path,message,evidence",
        ...audit.map((event) =>
          [event.occurred_at, event.level, event.stage, event.relative_path, event.message, event.evidence]
            .map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`)
            .join(",")
        )
      ].join("\r\n");
      return jsonBlob(`\ufeff${csv}`, "text/csv;charset=utf-8") as T;
    }
    return (path.includes("format=json")
      ? jsonBlob(audit, "application/json")
      : { events: audit, items: runItems }) as T;
  }

  throw new Error(`Demo API 未实现：${method} ${path}`);
}
