import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { AppSettings, AuthStatus, Project, RunSummary, ScanResult } from "./types";

const apiMock = vi.hoisted(() => ({
  isDemo: false,
  getSession: vi.fn(),
  health: vi.fn(),
  listWorkspaces: vi.fn(),
  selectWorkspace: vi.fn(),
  createWorkspace: vi.fn(),
  getSettings: vi.fn(),
  getAuthStatus: vi.fn(),
  listProjects: vi.fn(),
  getScan: vi.fn(),
  getPreflight: vi.fn(),
  getTree: vi.fn(),
  getPlan: vi.fn(),
  getAudit: vi.fn(),
  getRuntimeLogs: vi.fn(),
  getRun: vi.fn(),
  saveSettings: vi.fn(),
  startAuth: vi.fn(),
  verifyApp: vi.fn(),
  verifyOauth: vi.fn(),
  verifySource: vi.fn(),
  verifyTarget: vi.fn(),
  createProject: vi.fn(),
  updateProject: vi.fn(),
  startScan: vi.fn(),
  buildPlan: vi.fn(),
  startRun: vi.fn(),
  controlRun: vi.fn(),
  reconcile: vi.fn(),
  exportAudit: vi.fn()
}));

vi.mock("./api/client", () => ({
  ApiError: class ApiError extends Error {},
  api: apiMock
}));

const scopes = [
  "offline_access",
  "drive:drive",
  "drive:file:upload",
  "drive:quota_detail:read_one",
  "contact:user.employee_id:readonly"
];

const emptySettings: AppSettings = {
  app_id: "",
  redirect_uri: "http://127.0.0.1:8000/oauth/callback",
  scopes,
  app_secret_configured: false,
  upload_qps: 4,
  wiki_calls_per_minute: 100,
  daily_upload_budget: 0
};

const emptyAuth: AuthStatus = {
  configured: false,
  authorized: false,
  scopes: []
};

function rejectOptionalProjectReads() {
  apiMock.getScan.mockRejectedValue(new Error("no scan"));
  apiMock.getPreflight.mockRejectedValue(new Error("no preflight"));
  apiMock.getTree.mockRejectedValue(new Error("no tree"));
  apiMock.getPlan.mockRejectedValue(new Error("no plan"));
  apiMock.getAudit.mockRejectedValue(new Error("no audit"));
}

async function enterConfiguration() {
  expect(await screen.findByRole("heading", { name: "选择项目", level: 1 })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "进入配置" }));
  expect(await screen.findByRole("heading", { name: "配置", level: 1 })).toBeInTheDocument();
}

describe("配置优先迁移向导", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    apiMock.getSession.mockResolvedValue({ ready: true });
    apiMock.health.mockResolvedValue({ ok: true, version: "3.0-test" });
    apiMock.listWorkspaces.mockResolvedValue({
      projects_root: "D:\\Folder2FeishuDrive\\Projects",
      active_folder_name: "Test",
      items: [
        {
          folder_name: "Test",
          folder_path: "D:\\Folder2FeishuDrive\\Projects\\Test",
          project_name: "",
          project_count: 0,
          has_ledger: true,
          has_settings: true,
          active: true
        }
      ]
    });
    apiMock.getSettings.mockResolvedValue(emptySettings);
    apiMock.getAuthStatus.mockResolvedValue(emptyAuth);
    apiMock.listProjects.mockResolvedValue([]);
    apiMock.getRuntimeLogs.mockResolvedValue({ entries: [], next_after: 0, reset: false });
    apiMock.verifyApp.mockResolvedValue({
      ok: true,
      kind: "app",
      message: "应用凭据已由飞书确认",
      details: { credential_valid: true }
    });
    apiMock.verifyOauth.mockResolvedValue({
      ok: true,
      kind: "oauth",
      message: "用户授权固定操作身份与五项权限已验证：迁移管理员",
      details: { user_name: "迁移管理员", scope_count: 5 }
    });
    apiMock.verifySource.mockResolvedValue({
      ok: true,
      kind: "source",
      message: "本地根目录存在且根层可读取",
      details: { normalized_path: "D:\\TechStyle\\Team FabDazzle - 文档" }
    });
    apiMock.verifyTarget.mockResolvedValue({
      ok: true,
      kind: "target",
      message: "已读取云盘目标文件夹",
      details: {
        folder_token: "DriveFolderToken",
        child_count: 0,
        container_edit_requires_pilot: true
      }
    });
    rejectOptionalProjectReads();
  });

  it("首次打开只显示配置入口，并锁定盘点及后续步骤", async () => {
    render(<App />);

    await enterConfiguration();
    expect(screen.getByText("飞书应用与本机安全配置")).toBeInTheDocument();
    expect(screen.getByText("云盘上传速率")).toBeInTheDocument();
    expect(screen.getByText("唯一来源、唯一云盘落点与安全增量")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证应用配置" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证当前身份" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证并保存并发速率" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证本地目录配置" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证云盘文件夹" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证安全策略" })).toBeInTheDocument();

    const rail = screen.getByLabelText("迁移步骤");
    const scanStep = within(rail).getByText("盘点").closest("button");
    expect(scanStep).toBeDisabled();
    expect(within(rail).getByText("预检").closest("button")).toBeDisabled();
  });

  it("核心控制台标签统一使用中文展示", async () => {
    render(<App />);

    await enterConfiguration();
    expect(await screen.findByText("必要配置检查")).toBeInTheDocument();
    expect(screen.getByText("应用凭据")).toBeInTheDocument();
    expect(screen.getByText("固定操作身份")).toBeInTheDocument();
    expect(screen.getByText("速率控制")).toBeInTheDocument();
    expect(screen.getByText("单向迁移路径")).toBeInTheDocument();
    expect(screen.getByText("用户授权 等待中")).toBeInTheDocument();

    [
      "MANDATORY CONFIG GATE",
      "APPLICATION CREDENTIALS",
      "FIXED OPERATOR IDENTITY",
      "RATE CONTROL",
      "ONE-WAY MIGRATION ROUTE",
      "LOCALHOST ONLY",
      "READ ONLY"
    ].forEach((label) => expect(screen.queryByText(label)).not.toBeInTheDocument());
  });

  it("已有大目录的后台数据未返回时也立即显示主界面", async () => {
    const project: Project = {
      id: "project-large-inventory",
      name: "FabDazzle 全量迁移",
      source_root: "D:\\TechStyle\\Team FabDazzle - 文档",
      target_wiki_url: "https://example.feishu.cn/drive/folder/DriveFolderToken",
      create_wrapper: true,
      wrapper_name: "Team FabDazzle - 文档",
      mode: "safe_incremental"
    };
    apiMock.listProjects.mockResolvedValue([project]);
    apiMock.getScan.mockResolvedValue({
      scan_id: "large-scan",
      status: "COMPLETED",
      summary: {
        files: 51_527,
        folders: 6_536,
        bytes: 307_000_000_000,
        placeholders: 0,
        unreadable: 0,
        empty_files: 184,
        too_long_names: 0,
        max_depth: 9,
        max_siblings: 121,
        upload_calls: 104_606,
        estimated_days: 0,
        scan_complete: true
      },
      checks: [],
      tree: []
    });
    apiMock.getTree.mockReturnValue(new Promise(() => undefined));
    window.localStorage.setItem("folder2feishu:last-step", "scan");

    render(<App />);

    expect(await screen.findByRole("heading", { name: "盘点", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText("51,527")).toBeInTheDocument();
    expect(screen.getByText("正在生成目录预览")).toBeInTheDocument();
    expect(screen.queryByText("正在建立本机安全会话")).not.toBeInTheDocument();
  });

  it("已保存配置仍需真实逐项验证，通过后才开放盘点步骤", async () => {
    const settings: AppSettings = {
      ...emptySettings,
      app_id: "cli_configured",
      app_secret_configured: true
    };
    const auth: AuthStatus = {
      configured: true,
      authorized: true,
      user_name: "迁移管理员",
      scopes
    };
    const project: Project = {
      id: "project-ready",
      name: "FabDazzle 试迁",
      source_root: "D:\\TechStyle\\Team FabDazzle - 文档",
      target_wiki_url: "https://example.feishu.cn/drive/folder/DriveFolderToken",
      create_wrapper: true,
      wrapper_name: "Team FabDazzle - 文档",
      mode: "safe_incremental"
    };
    apiMock.getSettings.mockResolvedValue(settings);
    apiMock.getAuthStatus.mockResolvedValue(auth);
    apiMock.listProjects.mockResolvedValue([project]);
    apiMock.saveSettings.mockResolvedValue(settings);
    apiMock.updateProject.mockResolvedValue(project);

    render(<App />);

    await enterConfiguration();

    const rail = await screen.findByLabelText("迁移步骤");
    const scanStep = within(rail).getByText("盘点").closest("button");
    expect(scanStep).toBeDisabled();

    fireEvent.click(screen.getAllByRole("button", { name: "一键验证全部" })[0]);
    await waitFor(() => expect(scanStep).toBeEnabled());
    expect(apiMock.verifyApp).toHaveBeenCalledTimes(1);
    expect(apiMock.verifyOauth).toHaveBeenCalledTimes(1);
    expect(apiMock.verifySource).toHaveBeenCalledWith(project.source_root);
    expect(apiMock.verifyTarget).toHaveBeenCalledWith(project.target_wiki_url);
    if (scanStep) fireEvent.click(scanStep);
    expect(await screen.findByRole("heading", { name: "盘点", level: 1 })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "开始只读盘点" })).toHaveLength(2);
    screen
      .getAllByRole("button", { name: "开始只读盘点" })
      .forEach((button) => expect(button).toBeEnabled());
  });

  it("正在盘点时持续等待并禁止重复启动，不提前执行预检", async () => {
    const settings: AppSettings = {
      ...emptySettings,
      app_id: "cli_configured",
      app_secret_configured: true
    };
    const auth: AuthStatus = {
      configured: true,
      authorized: true,
      user_name: "迁移管理员",
      scopes
    };
    const project: Project = {
      id: "project-scanning",
      name: "FabDazzle 全量盘点",
      source_root: "D:\\TechStyle\\Team FabDazzle - 文档",
      target_wiki_url: "https://example.feishu.cn/drive/folder/DriveFolderToken",
      create_wrapper: true,
      wrapper_name: "Team FabDazzle - 文档",
      mode: "safe_incremental"
    };
    const activeScan: ScanResult = {
      scan_id: "scan-active",
      status: "RUNNING",
      summary: {
        files: 403,
        folders: 97,
        bytes: 3_400_000_000,
        placeholders: 0,
        unreadable: 0,
        empty_files: 0,
        too_long_names: 0,
        max_depth: 7,
        max_siblings: 154,
        upload_calls: 1_089,
        estimated_days: 1,
        scan_complete: false
      },
      checks: [],
      tree: []
    };
    apiMock.getSettings.mockResolvedValue(settings);
    apiMock.getAuthStatus.mockResolvedValue(auth);
    apiMock.listProjects.mockResolvedValue([project]);
    apiMock.getScan.mockResolvedValue(activeScan);
    apiMock.saveSettings.mockResolvedValue(settings);
    apiMock.updateProject.mockResolvedValue(project);

    render(<App />);

    await enterConfiguration();

    const rail = await screen.findByLabelText("迁移步骤");
    const scanStep = within(rail).getByText("盘点").closest("button");
    fireEvent.click(screen.getAllByRole("button", { name: "一键验证全部" })[0]);
    await waitFor(() => expect(scanStep).toBeEnabled());
    if (scanStep) fireEvent.click(scanStep);

    const activeButtons = await screen.findAllByRole("button", { name: "盘点进行中" });
    activeButtons.forEach((button) => expect(button).toBeDisabled());
    expect(apiMock.getPreflight).not.toHaveBeenCalled();
    expect(apiMock.startScan).not.toHaveBeenCalled();
  });

  it("浏览器刷新后恢复原步骤，页面内刷新不跳回配置", async () => {
    const settings: AppSettings = {
      ...emptySettings,
      app_id: "cli_configured",
      app_secret_configured: true
    };
    const auth: AuthStatus = {
      configured: true,
      authorized: true,
      user_name: "迁移管理员",
      scopes
    };
    const project: Project = {
      id: "project-persisted-step",
      name: "JF 文档迁移",
      source_root: "D:\\TechStyle\\Team FabDazzle - 文档",
      target_wiki_url: "https://example.feishu.cn/drive/folder/DriveFolderToken",
      create_wrapper: true,
      wrapper_name: "Team FabDazzle - 文档",
      mode: "safe_incremental"
    };
    const completedScan: ScanResult = {
      scan_id: "scan-completed",
      status: "COMPLETED",
      summary: {
        files: 403,
        folders: 97,
        bytes: 3_400_000_000,
        placeholders: 0,
        unreadable: 0,
        empty_files: 0,
        too_long_names: 0,
        max_depth: 7,
        max_siblings: 154,
        upload_calls: 1_089,
        estimated_days: 1,
        scan_complete: true
      },
      checks: [],
      tree: []
    };
    window.localStorage.setItem("folder2feishu:last-step", "scan");
    apiMock.getSettings.mockResolvedValue(settings);
    apiMock.getAuthStatus.mockResolvedValue(auth);
    apiMock.listProjects.mockResolvedValue([project]);
    apiMock.getScan.mockResolvedValue(completedScan);
    apiMock.getPreflight.mockResolvedValue({
      complete: true,
      writable: true,
      checked_at: "2026-07-30T08:00:00Z",
      checks: []
    });
    apiMock.getTree.mockResolvedValue([]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "盘点", level: 1 })).toBeInTheDocument();
    expect(window.localStorage.getItem("folder2feishu:last-step")).toBe("scan");

    fireEvent.click(screen.getByRole("button", { name: "刷新当前页面数据" }));

    await waitFor(() => expect(apiMock.health).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("heading", { name: "盘点", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText("当前页面数据已刷新，所在步骤保持不变。")).toBeInTheDocument();
  });

  it("运行页直接显示大文件分片字节进度和实时飞书日志", async () => {
    const project: Project = {
      id: "project-live-upload",
      name: "JF 文档迁移",
      source_root: "D:\\TechStyle\\Team FabDazzle - 文档",
      target_wiki_url: "https://example.feishu.cn/drive/folder/DriveFolderToken",
      create_wrapper: true,
      wrapper_name: "Team FabDazzle - 文档",
      mode: "safe_incremental",
      last_run_id: "run-live"
    };
    const run: RunSummary = {
      id: "run-live",
      project_id: project.id,
      kind: "MIGRATION",
      state: "RUNNING",
      stage: "MIGRATING_UPLOAD",
      current_path: "FabKids\\Training.mp4",
      total: 100,
      completed: 40,
      failed: 0,
      conflicts: 0,
      bytes_total: 1024 * 1024 * 1024,
      bytes_completed: 400 * 1024 * 1024,
      active_uploads: [
        {
          action_id: "upload-action",
          relative_path: "FabKids\\Training.mp4",
          status: "UPLOADING",
          completed_parts: 25,
          total_parts: 100,
          uploaded_bytes: 100 * 1024 * 1024,
          total_bytes: 400 * 1024 * 1024,
          percent: 25,
          attempts: 25,
          updated_at: "2026-08-04T02:31:12Z"
        }
      ],
      quota: {
        upload_calls_used: 100,
        upload_calls_limit: 0,
        wiki_calls_minute: 0,
        wiki_calls_limit: 80
      }
    };
    window.localStorage.setItem("folder2feishu:last-step", "run");
    apiMock.listProjects.mockResolvedValue([project]);
    apiMock.getScan.mockRejectedValue(new Error("not needed"));
    apiMock.getTree.mockResolvedValue([]);
    apiMock.getPlan.mockRejectedValue(new Error("not needed"));
    apiMock.getAudit.mockResolvedValue({ events: [], items: [] });
    apiMock.getRun.mockResolvedValue(run);
    apiMock.getRuntimeLogs.mockResolvedValue({
      entries: [
        {
          id: "log-live",
          occurred_at: "2026-08-04T02:31:12Z",
          level: "INFO",
          logger: "httpx",
          message: 'HTTP Request: POST https://open.feishu.cn/open-apis/drive/v1/files/upload_part "HTTP/1.1 200 OK"'
        }
      ],
      next_after: 100,
      reset: false
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "运行对账", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText("大文件分片进度")).toBeInTheDocument();
    expect(screen.getByText("25 / 100 分片")).toBeInTheDocument();
    expect(screen.getByText("100 MB / 400 MB")).toBeInTheDocument();
    expect(await screen.findByText("分片上传成功")).toBeInTheDocument();
  });
});
