import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { AppSettings, AuthStatus, Project } from "./types";

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
  getSchedule: vi.fn(),
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
  exportAudit: vi.fn(),
  saveSchedule: vi.fn()
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
  daily_upload_budget: 9_500
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
  apiMock.getSchedule.mockRejectedValue(new Error("no schedule"));
  apiMock.getAudit.mockRejectedValue(new Error("no audit"));
}

describe("配置优先迁移向导", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    expect(screen.getByText("上传限流与每日调用预算")).toBeInTheDocument();
    expect(screen.getByText("定时安全增量盘点")).toBeInTheDocument();
    expect(screen.getByText("唯一来源、唯一落点与安全增量")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证应用配置" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证当前身份" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证并保存调用限额" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证本地目录配置" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证知识库地址" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证安全策略" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "验证定时盘点设置" })).toBeInTheDocument();

    const rail = screen.getByLabelText("迁移步骤");
    const scanStep = within(rail).getByText("盘点").closest("button");
    expect(scanStep).toBeDisabled();
    expect(within(rail).getByText("预检").closest("button")).toBeDisabled();
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
      mode: "safe_incremental",
      schedule_enabled: false
    };
    apiMock.getSettings.mockResolvedValue(settings);
    apiMock.getAuthStatus.mockResolvedValue(auth);
    apiMock.listProjects.mockResolvedValue([project]);
    apiMock.getSchedule.mockResolvedValue({ enabled: false, local_time: "02:30" });
    apiMock.saveSettings.mockResolvedValue(settings);
    apiMock.updateProject.mockResolvedValue(project);
    apiMock.saveSchedule.mockResolvedValue({ enabled: false, local_time: "02:30" });

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
});
