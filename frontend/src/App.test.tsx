import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { AppSettings, AuthStatus, Project, ScanResult } from "./types";

const apiMock = vi.hoisted(() => ({
  isDemo: false,
  getSession: vi.fn(),
  health: vi.fn(),
  getSettings: vi.fn(),
  getAuthStatus: vi.fn(),
  listProjects: vi.fn(),
  getScan: vi.fn(),
  getPreflight: vi.fn(),
  getTree: vi.fn(),
  getPlan: vi.fn(),
  getAudit: vi.fn(),
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
  "wiki:wiki",
  "drive:quota_detail:read_one",
  "contact:user.employee_id:readonly"
];

const emptySettings: AppSettings = {
  app_id: "",
  redirect_uri: "http://127.0.0.1:8000/oauth/callback",
  scopes,
  app_secret_configured: false,
  upload_qps: 4,
  wiki_calls_per_minute: 90,
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

describe("配置优先迁移向导", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    apiMock.getSession.mockResolvedValue({ ready: true });
    apiMock.health.mockResolvedValue({ ok: true, version: "2.0-test" });
    apiMock.getSettings.mockResolvedValue(emptySettings);
    apiMock.getAuthStatus.mockResolvedValue(emptyAuth);
    apiMock.listProjects.mockResolvedValue([]);
    apiMock.verifyApp.mockResolvedValue({
      ok: true,
      kind: "app",
      message: "应用凭据已由飞书确认",
      details: { credential_valid: true }
    });
    apiMock.verifyOauth.mockResolvedValue({
      ok: true,
      kind: "oauth",
      message: "OAuth 固定操作身份与六项权限已验证：迁移管理员",
      details: { user_name: "迁移管理员", scope_count: 6 }
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
      message: "已读取知识库目标并确认页面编辑权限",
      details: {
        space_id: "space",
        node_token: "WikiParentToken",
        page_editable: true,
        container_edit_requires_pilot: true
      }
    });
    rejectOptionalProjectReads();
  });

  it("首次打开只显示配置入口，并锁定盘点及后续步骤", async () => {
    render(<App />);

    expect(await screen.findByRole("heading", { name: "配置", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("飞书应用与本机安全配置")).toBeInTheDocument();
    expect(screen.getByText("上传与知识库并发速率")).toBeInTheDocument();
    expect(screen.getByText("唯一来源、唯一落点与安全增量")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证应用配置" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证当前身份" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证并保存并发速率" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证本地目录配置" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证知识库地址" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证安全策略" })).toBeInTheDocument();

    const rail = screen.getByLabelText("迁移步骤");
    const scanStep = within(rail).getByText("盘点").closest("button");
    expect(scanStep).toBeDisabled();
    expect(within(rail).getByText("预检").closest("button")).toBeDisabled();
  });

  it("已有大目录的后台数据未返回时也立即显示主界面", async () => {
    const project: Project = {
      id: "project-large-inventory",
      name: "FabDazzle 全量迁移",
      source_root: "D:\\TechStyle\\Team FabDazzle - 文档",
      target_wiki_url: "https://example.feishu.cn/wiki/WikiParentToken",
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

    render(<App />);

    expect(await screen.findByRole("heading", { name: "配置", level: 1 })).toBeInTheDocument();
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
      target_wiki_url: "https://example.feishu.cn/wiki/WikiParentToken",
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
      target_wiki_url: "https://example.feishu.cn/wiki/WikiParentToken",
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
      target_wiki_url: "https://example.feishu.cn/wiki/WikiParentToken",
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
});
