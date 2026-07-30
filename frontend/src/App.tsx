import {
  AlertTriangle,
  ArrowRight,
  BadgeCheck,
  Boxes,
  Check,
  CheckCircle2,
  ChevronRight,
  CirclePause,
  CirclePlay,
  Cloud,
  Database,
  Download,
  ExternalLink,
  File,
  FileCheck2,
  FileClock,
  FileWarning,
  Folder,
  FolderTree,
  Gauge,
  HardDrive,
  KeyRound,
  ListFilter,
  LoaderCircle,
  LockKeyhole,
  OctagonX,
  PanelTop,
  Play,
  RefreshCcw,
  RotateCcw,
  Route,
  ScanLine,
  SearchCheck,
  Server,
  Settings2,
  ShieldCheck,
  Square,
  UploadCloud,
  Waypoints
} from "lucide-react";
import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api } from "./api/client";
import type {
  AppSettings,
  AuditEvent,
  AuthStatus,
  MigrationPlan,
  PlannedActionKind,
  PreflightResult,
  Project,
  ProjectDraft,
  RunItem,
  RunSummary,
  ScanResult,
  Severity,
  StepId,
  TreeNode
} from "./types";
import {
  actionLabel,
  actionTone,
  downloadBlob,
  formatBytes,
  formatEta,
  formatPercent
} from "./utils";

const DEFAULT_SCOPES = [
  "offline_access",
  "drive:drive",
  "drive:file:upload",
  "wiki:wiki",
  "drive:quota_detail:read_one",
  "contact:user.employee_id:readonly"
];

const steps: Array<{
  id: StepId;
  no: string;
  eyebrow: string;
  label: string;
  description: string;
  icon: typeof KeyRound;
}> = [
  {
    id: "config",
    no: "01",
    eyebrow: "CONFIG",
    label: "配置",
    description: "集中填写与逐项验证",
    icon: Settings2
  },
  {
    id: "scan",
    no: "02",
    eyebrow: "INVENTORY",
    label: "盘点",
    description: "只读扫描本地目录",
    icon: ScanLine
  },
  {
    id: "preflight",
    no: "03",
    eyebrow: "GUARD",
    label: "预检",
    description: "权限、容量与文件",
    icon: ShieldCheck
  },
  {
    id: "plan",
    no: "04",
    eyebrow: "DIFF",
    label: "差异计划",
    description: "确认每一项写操作",
    icon: Waypoints
  },
  {
    id: "run",
    no: "05",
    eyebrow: "CONTROL",
    label: "运行对账",
    description: "断点、配额与证据",
    icon: Gauge
  }
];

const STEP_STORAGE_KEY = "folder2feishu:last-step";
const validSteps = new Set<StepId>(steps.map((item) => item.id));

const readSavedStep = (): StepId => {
  try {
    const saved = window.localStorage.getItem(STEP_STORAGE_KEY) as StepId | null;
    return saved && validSteps.has(saved) ? saved : "config";
  } catch {
    return "config";
  }
};

const severityIcon: Record<Severity, typeof CheckCircle2> = {
  ok: CheckCircle2,
  warning: AlertTriangle,
  error: OctagonX,
  info: SearchCheck
};

const statusLabel: Record<string, string> = {
  IDLE: "尚未运行",
  RUNNING: "正在迁移",
  PAUSED: "已暂停",
  COMPLETED: "迁移完成",
  FAILED: "运行失败",
  STOPPED: "已停止",
  DISCOVERED: "已盘点",
  PLANNED: "已计划",
  UPLOADING: "上传中",
  DRIVE_UPLOADED: "中转完成",
  WIKI_MOVING: "迁入知识库",
  VERIFYING: "远端核验",
  DONE: "已完成",
  RETRYABLE: "可重试",
  CONFLICT: "人工冲突",
  MANUAL_ACTION: "人工处理"
};

const emptySettings: AppSettings = {
  app_id: "",
  redirect_uri: "http://127.0.0.1:8000/oauth/callback",
  scopes: DEFAULT_SCOPES,
  app_secret_configured: false,
  upload_qps: 4,
  wiki_calls_per_minute: 90,
  daily_upload_budget: 9_500
};

const emptyDraft: ProjectDraft = {
  name: "JF 文档迁移",
  source_root: "",
  target_wiki_url: "",
  create_wrapper: true,
  wrapper_name: ""
};

type ValidationKey =
  | "app"
  | "oauth"
  | "throttle"
  | "source"
  | "target"
  | "policy";
type ValidationStatus = "idle" | "checking" | "passed" | "failed";
type ValidationState = Record<
  ValidationKey,
  { status: ValidationStatus; message: string }
>;

const emptyValidation: ValidationState = {
  app: { status: "idle", message: "尚未验证应用配置" },
  oauth: { status: "idle", message: "尚未验证固定操作身份" },
  throttle: { status: "idle", message: "尚未验证限流与每日预算" },
  source: { status: "idle", message: "尚未检查本地根目录配置" },
  target: { status: "idle", message: "尚未检查知识库地址" },
  policy: { status: "idle", message: "尚未检查增量策略" }
};

function Panel({
  children,
  className = "",
  tone = ""
}: {
  children: ReactNode;
  className?: string;
  tone?: "" | "green" | "amber" | "red";
}) {
  return <section className={`panel ${tone ? `panel--${tone}` : ""} ${className}`}>{children}</section>;
}

function PanelHeading({
  eyebrow,
  title,
  copy,
  icon: Icon,
  tools
}: {
  eyebrow: string;
  title: string;
  copy?: string;
  icon?: typeof KeyRound;
  tools?: ReactNode;
}) {
  return (
    <div className="panel-heading">
      <div className="panel-heading__mark">{Icon ? <Icon size={18} aria-hidden="true" /> : null}</div>
      <div className="panel-heading__text">
        <span className="eyebrow">{eyebrow}</span>
        <h2>{title}</h2>
        {copy ? <p>{copy}</p> : null}
      </div>
      {tools ? <div className="panel-heading__tools">{tools}</div> : null}
    </div>
  );
}

function Button({
  children,
  icon: Icon,
  variant = "secondary",
  busy = false,
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  icon?: typeof KeyRound;
  variant?: "primary" | "secondary" | "danger" | "ghost";
  busy?: boolean;
}) {
  return (
    <button
      className={`button button--${variant} ${className}`}
      {...props}
      disabled={busy || props.disabled}
    >
      {busy ? (
        <LoaderCircle className="spin" size={16} aria-hidden="true" />
      ) : Icon ? (
        <Icon size={16} aria-hidden="true" />
      ) : null}
      <span>{children}</span>
    </button>
  );
}

function Metric({
  label,
  value,
  note,
  icon: Icon,
  tone = ""
}: {
  label: string;
  value: string | number;
  note?: string;
  icon: typeof KeyRound;
  tone?: "green" | "amber" | "red" | "";
}) {
  return (
    <div className={`metric ${tone ? `metric--${tone}` : ""}`}>
      <div className="metric__icon">
        <Icon size={19} aria-hidden="true" />
      </div>
      <div>
        <span className="metric__label">{label}</span>
        <strong>{value}</strong>
        {note ? <small>{note}</small> : null}
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
  required
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  required?: boolean;
}) {
  return (
    <label className="field">
      <span className="field__label">
        {label}
        {required ? <b aria-hidden="true">*</b> : null}
      </span>
      {children}
      {hint ? <small>{hint}</small> : null}
    </label>
  );
}

function Toggle({
  checked,
  onChange,
  label,
  description
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  description: string;
}) {
  return (
    <label className="toggle-row">
      <span>
        <strong>{label}</strong>
        <small>{description}</small>
      </span>
      <span className={`toggle ${checked ? "is-on" : ""}`}>
        <input
          type="checkbox"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span aria-hidden="true" />
      </span>
    </label>
  );
}

function ValidationBadge({
  status,
  message
}: {
  status: ValidationStatus;
  message: string;
}) {
  const Icon =
    status === "passed"
      ? CheckCircle2
      : status === "failed"
        ? AlertTriangle
        : status === "checking"
          ? LoaderCircle
          : SearchCheck;
  return (
    <span className={`validation-badge is-${status}`} title={message}>
      <Icon className={status === "checking" ? "spin" : ""} size={14} aria-hidden="true" />
      {status === "passed"
        ? "验证通过"
        : status === "failed"
          ? "需要处理"
          : status === "checking"
            ? "验证中"
            : "待验证"}
    </span>
  );
}

function EmptyState({
  icon: Icon,
  title,
  copy,
  action
}: {
  icon: typeof KeyRound;
  title: string;
  copy: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-state__signal">
        <Icon size={28} aria-hidden="true" />
      </div>
      <h3>{title}</h3>
      <p>{copy}</p>
      {action}
    </div>
  );
}

function TreeBranch({ node, level = 0 }: { node: TreeNode; level?: number }) {
  const hasChildren = Boolean(node.children?.length);
  const Icon = node.kind === "folder" ? Folder : File;
  if (node.kind === "folder") {
    return (
      <details className="tree-branch" open={level < 2}>
        <summary>
          <span className="tree-chevron">
            {hasChildren ? <ChevronRight size={14} aria-hidden="true" /> : <span />}
          </span>
          <Icon size={16} aria-hidden="true" />
          <span className="tree-name">{node.name}</span>
          <small>{node.children?.length ?? 0} 项</small>
        </summary>
        {hasChildren ? (
          <div className="tree-children">
            {node.children?.map((child) => (
              <TreeBranch key={child.id} node={child} level={level + 1} />
            ))}
          </div>
        ) : null}
      </details>
    );
  }
  return (
    <div className="tree-file">
      <span className="tree-chevron" />
      <Icon size={15} aria-hidden="true" />
      <span className="tree-name">{node.name}</span>
      <small>{formatBytes(node.size)}</small>
    </div>
  );
}

function App() {
  const [step, setStep] = useState<StepId>(readSavedStep);
  const [version, setVersion] = useState("2.0");
  const [settings, setSettings] = useState<AppSettings>(emptySettings);
  const [secret, setSecret] = useState("");
  const [auth, setAuth] = useState<AuthStatus>({
    configured: false,
    authorized: false,
    scopes: []
  });
  const [project, setProject] = useState<Project>();
  const [draft, setDraft] = useState<ProjectDraft>(emptyDraft);
  const [scan, setScan] = useState<ScanResult>();
  const [preflight, setPreflight] = useState<PreflightResult>();
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [plan, setPlan] = useState<MigrationPlan>();
  const [run, setRun] = useState<RunSummary>();
  const [runItems, setRunItems] = useState<RunItem[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [validation, setValidation] = useState<ValidationState>(emptyValidation);
  const [actionFilter, setActionFilter] = useState<PlannedActionKind | "ALL">("ALL");
  const [runFilter, setRunFilter] = useState<"ALL" | "FAILED" | "ACTIVE">("ALL");
  const [busy, setBusy] = useState("");
  const [booting, setBooting] = useState(true);
  const [notice, setNotice] = useState<{ tone: Severity; text: string }>();

  const notify = useCallback((text: string, tone: Severity = "ok") => {
    setNotice({ text, tone });
    window.setTimeout(() => setNotice(undefined), 4200);
  }, []);

  const showError = useCallback(
    (error: unknown) => {
      const message =
        error instanceof ApiError || error instanceof Error ? error.message : "操作没有完成，请重试。";
      notify(message, "error");
    },
    [notify]
  );

  const loadProjectData = useCallback(
    async (activeProject: Project) => {
      const scanCall = await Promise.allSettled([api.getScan(activeProject.id)]);
      const scanResult = scanCall[0].status === "fulfilled" ? scanCall[0].value : undefined;
      if (scanResult) setScan(scanResult);
      const calls = await Promise.allSettled([
        scanResult?.summary.scan_complete
          ? api.getPreflight(activeProject.id)
          : Promise.resolve(undefined),
        api.getTree(activeProject.id),
        api.getPlan(activeProject.id),
        api.getAudit(activeProject.id),
        activeProject.last_run_id ? api.getRun(activeProject.last_run_id) : Promise.resolve(undefined)
      ]);
      if (calls[0].status === "fulfilled" && calls[0].value) setPreflight(calls[0].value);
      if (calls[1].status === "fulfilled") setTree(calls[1].value);
      if (calls[2].status === "fulfilled") setPlan(calls[2].value);
      if (calls[3].status === "fulfilled") {
        const audit = calls[3].value as { events?: AuditEvent[]; items?: RunItem[] };
        setEvents(audit.events ?? []);
        setRunItems(audit.items ?? []);
      }
      if (calls[4].status === "fulfilled" && calls[4].value) setRun(calls[4].value);
    },
    []
  );

  useEffect(() => {
    try {
      window.localStorage.setItem(STEP_STORAGE_KEY, step);
    } catch {
      // Navigation persistence is a convenience only; the migration must work without it.
    }
  }, [step]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        await api.getSession();
        const [health, currentSettings, currentAuth, projects] = await Promise.all([
          api.health(),
          api.getSettings(),
          api.getAuthStatus(),
          api.listProjects()
        ]);
        if (!alive) return;
        setVersion(health.version);
        setSettings(currentSettings);
        setAuth(currentAuth);
        const activeProject = projects[0];
        setValidation({
          app: {
            status: "idle",
            message:
              currentSettings.app_id && currentSettings.app_secret_configured
                ? "应用凭据已保存，请点击按钮向飞书验证"
                : "请填写应用 ID、Secret 和回调地址"
          },
          oauth: {
            status: "idle",
            message: currentAuth.authorized
              ? "已发现 OAuth 授权，请点击按钮回读身份与六项权限"
              : "请完成飞书 OAuth 授权"
          },
          throttle: {
            status:
              currentSettings.upload_qps > 0 &&
              currentSettings.upload_qps <= 4 &&
              currentSettings.wiki_calls_per_minute >= 1 &&
              currentSettings.wiki_calls_per_minute <= 90 &&
              currentSettings.daily_upload_budget >= 1 &&
              currentSettings.daily_upload_budget <= 9_500
                ? "passed"
                : "idle",
            message: "限流与每日预算已加载，仍受飞书服务端配额约束"
          },
          source: {
            status: "idle",
            message: activeProject?.source_root
              ? "本地根目录已保存，请点击按钮做只读可读性检查"
              : "请填写 Windows 本地绝对路径"
          },
          target: {
            status: "idle",
            message: activeProject?.target_wiki_url
              ? "知识库 URL 已保存，请点击按钮读取节点并检查页面编辑权限"
              : "请填写飞书知识库 URL"
          },
          policy: {
            status: activeProject ? "passed" : "idle",
            message: activeProject
              ? "安全增量与同名根节点策略已固定"
              : "请确认安全增量选项"
          }
        });
        if (activeProject) {
          setProject(activeProject);
          setDraft({
            name: activeProject.name,
            source_root: activeProject.source_root,
            target_wiki_url: activeProject.target_wiki_url,
            create_wrapper: true,
            wrapper_name: activeProject.wrapper_name ?? ""
          });
          await loadProjectData(activeProject);
        } else {
          setStep("config");
        }
      } catch (error) {
        if (alive) showError(error);
      } finally {
        if (alive) setBooting(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [loadProjectData, showError]);

  useEffect(() => {
    if (!run || run.state !== "RUNNING") return;
    const timer = window.setInterval(async () => {
      try {
        setRun(await api.getRun(run.id));
      } catch {
        // A transient polling failure should not interrupt the migration itself.
      }
    }, 4_000);
    return () => window.clearInterval(timer);
  }, [run?.id, run?.state]);

  useEffect(() => {
    if (
      !project ||
      (scan?.status !== "RUNNING" && scan?.status !== "PENDING")
    ) return;
    let alive = true;
    let polling = false;
    const refreshScan = async () => {
      if (polling) return;
      polling = true;
      try {
        const result = await api.getScan(project.id);
        if (!alive) return;
        if (result.status === "COMPLETED" && result.summary.scan_complete) {
          const [guard, remoteTree] = await Promise.all([
            api.getPreflight(project.id),
            api.getTree(project.id)
          ]);
          if (!alive) return;
          setPreflight(guard);
          setTree(remoteTree.length ? remoteTree : result.tree);
          setScan(result);
          notify("盘点完成。请检查目录规模与问题清单，再进入预检。");
        } else {
          setScan(result);
          if (result.status === "FAILED") {
            notify("盘点未完成，请查看本地完整性检查和日志后重试。", "error");
          }
        }
      } catch {
        // A short local API interruption must not turn an active scan into a failure.
      } finally {
        polling = false;
      }
    };
    void refreshScan();
    const timer = window.setInterval(refreshScan, 1_500);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [project?.id, scan?.status, notify]);

  const markValidation = (
    key: ValidationKey,
    status: ValidationStatus,
    message: string
  ) => {
    setValidation((current) => ({ ...current, [key]: { status, message } }));
  };

  const throttleConfigurationValid = (): boolean =>
    Number.isFinite(settings.upload_qps) &&
    settings.upload_qps > 0 &&
    settings.upload_qps <= 4 &&
    Number.isInteger(settings.wiki_calls_per_minute) &&
    settings.wiki_calls_per_minute >= 1 &&
    settings.wiki_calls_per_minute <= 90 &&
    Number.isInteger(settings.daily_upload_budget) &&
    settings.daily_upload_budget >= 1 &&
    settings.daily_upload_budget <= 9_500;

  const validateApp = async (): Promise<boolean> => {
    markValidation("app", "checking", "正在由后端校验并安全保存应用配置…");
    if (!settings.app_id.trim() || (!settings.app_secret_configured && !secret.trim())) {
      markValidation("app", "failed", "必须填写 App ID；首次配置还必须填写 App Secret");
      return false;
    }
    if (!throttleConfigurationValid()) {
      markValidation("app", "failed", "后端会原子保存全部设置，请先修正限流与每日预算");
      return false;
    }
    try {
      const saved = await api.saveSettings({
        app_id: settings.app_id,
        app_secret: secret || undefined,
        redirect_uri: settings.redirect_uri,
        scopes: DEFAULT_SCOPES,
        upload_qps: settings.upload_qps,
        wiki_calls_per_minute: settings.wiki_calls_per_minute,
        daily_upload_budget: settings.daily_upload_budget
      });
      setSettings(saved);
      setSecret("");
      const nextAuth = await api.getAuthStatus();
      setAuth(nextAuth);
      const result = await api.verifyApp();
      markValidation("app", "passed", result.message);
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : "应用配置验证失败";
      markValidation("app", "failed", message);
      return false;
    }
  };

  const validateThrottle = async (): Promise<boolean> => {
    markValidation("throttle", "checking", "正在验证上传节流、Wiki 频率和每日预算…");
    if (!throttleConfigurationValid()) {
      markValidation(
        "throttle",
        "failed",
        "范围必须为：0 < 上传 QPS ≤ 4、Wiki 1–90 次/分钟、每日 1–9500 次"
      );
      return false;
    }
    if (!settings.app_id.trim() || (!settings.app_secret_configured && !secret.trim())) {
      markValidation("throttle", "failed", "请先填写飞书 App ID 和 App Secret，后端才能保存设置");
      return false;
    }
    try {
      const saved = await api.saveSettings({
        app_id: settings.app_id,
        app_secret: secret || undefined,
        redirect_uri: settings.redirect_uri,
        scopes: DEFAULT_SCOPES,
        upload_qps: settings.upload_qps,
        wiki_calls_per_minute: settings.wiki_calls_per_minute,
        daily_upload_budget: settings.daily_upload_budget
      });
      setSettings(saved);
      setSecret("");
      markValidation(
        "throttle",
        "passed",
        `${saved.upload_qps} QPS · ${saved.wiki_calls_per_minute} Wiki/分钟 · ${saved.daily_upload_budget} 上传/日`
      );
      return true;
    } catch (error) {
      markValidation(
        "throttle",
        "failed",
        error instanceof Error ? error.message : "限流与每日预算保存失败"
      );
      return false;
    }
  };

  const validateOauth = async (): Promise<boolean> => {
    markValidation("oauth", "checking", "正在回读 OAuth 用户身份与权限范围…");
    try {
      const result = await api.verifyOauth();
      const nextAuth = await api.getAuthStatus();
      setAuth(nextAuth);
      markValidation("oauth", "passed", result.message);
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : "OAuth 身份验证失败";
      markValidation("oauth", "failed", message);
      return false;
    }
  };

  const beginAuth = async () => {
    setBusy("oauth");
    try {
      const result = await api.startAuth();
      window.location.assign(result.authorization_url);
    } catch (error) {
      showError(error);
      setBusy("");
    }
  };

  const sourceConfigurationValid = (): boolean =>
    /^[A-Za-z]:\\/.test(draft.source_root.trim()) ||
    /^\\\\[^\\]+\\[^\\]+/.test(draft.source_root.trim());

  const targetConfigurationValid = (): boolean => {
    try {
      const target = new URL(draft.target_wiki_url.trim());
      return (
        ["https:"].includes(target.protocol) &&
        (target.hostname.endsWith(".feishu.cn") ||
          target.hostname.endsWith(".larksuite.com")) &&
        /^\/wiki\/[A-Za-z0-9]+/.test(target.pathname)
      );
    } catch {
      return false;
    }
  };

  const persistProject = async (silent = false): Promise<Project | undefined> => {
    if (!draft.source_root.trim() || !draft.target_wiki_url.trim()) {
      if (!silent) notify("请填写本地源目录和飞书知识库地址。", "warning");
      return undefined;
    }
    const saved = project
      ? await api.updateProject(project.id, draft)
      : await api.createProject(draft);
    setProject(saved);
    if (!silent) notify(project ? "迁移配置已更新。" : "迁移项目已创建。");
    return saved;
  };

  const validateSource = async (): Promise<boolean> => {
    markValidation("source", "checking", "正在检查 Windows 本地根目录配置…");
    if (!sourceConfigurationValid()) {
      markValidation("source", "failed", "请输入盘符绝对路径或 UNC 路径");
      return false;
    }
    try {
      const result = await api.verifySource(draft.source_root.trim());
      markValidation("source", "passed", result.message);
      if (targetConfigurationValid()) {
        await persistProject(true);
      }
    } catch (error) {
      markValidation(
        "source",
        "failed",
        error instanceof Error ? error.message : "本地根目录验证失败"
      );
      return false;
    }
    return true;
  };

  const validateTarget = async (): Promise<boolean> => {
    markValidation("target", "checking", "正在检查飞书知识库 URL 配置…");
    if (!targetConfigurationValid()) {
      markValidation(
        "target",
        "failed",
        "必须填写 https://*.feishu.cn/wiki/... 或 larksuite.com/wiki/... 地址"
      );
      return false;
    }
    try {
      const result = await api.verifyTarget(draft.target_wiki_url.trim());
      if (sourceConfigurationValid()) await persistProject(true);
      markValidation("target", "passed", result.message);
      return true;
    } catch (error) {
      markValidation(
        "target",
        "failed",
        error instanceof Error ? error.message : "后端未接受知识库配置"
      );
      return false;
    }
  };

  const validatePolicy = async (): Promise<boolean> => {
    markValidation("policy", "checking", "正在检查根节点与安全增量策略…");
    if (!draft.create_wrapper) {
      markValidation("policy", "failed", "知识库迁移必须创建同名根包装节点");
      return false;
    }
    if ((draft.wrapper_name?.length ?? 0) > 250) {
      markValidation("policy", "failed", "根节点名称不能超过 250 个字符");
      return false;
    }
    markValidation(
      "policy",
      "passed",
      "安全增量已锁定：变更留历史、本地删除只报告、远端冲突不覆盖"
    );
    return true;
  };

  const validateAll = async (event?: FormEvent): Promise<void> => {
    event?.preventDefault();
    setBusy("validate-all");
    try {
      const appOk = await validateApp();
      const throttleOk = await validateThrottle();
      const oauthOk = await validateOauth();
      const sourceOk = await validateSource();
      const targetOk = await validateTarget();
      const policyOk = await validatePolicy();
      let projectOk = Boolean(project);
      let savedProject = project;
      if (sourceOk && targetOk && policyOk) {
        try {
          savedProject = await persistProject(true);
          projectOk = Boolean(savedProject);
        } catch (error) {
          const message = error instanceof Error ? error.message : "项目配置保存失败";
          markValidation("source", "failed", message);
          markValidation("target", "failed", message);
          projectOk = false;
        }
      }
      const allPassed =
        appOk &&
        throttleOk &&
        oauthOk &&
        sourceOk &&
        targetOk &&
        policyOk &&
        projectOk;
      notify(
        allPassed
          ? "必要配置已全部验证，可以进入只读盘点。"
          : "验证完成，请处理标红项目后再次验证。",
        allPassed ? "ok" : "warning"
      );
    } finally {
      setBusy("");
    }
  };

  const startScan = async () => {
    if (!configReady) {
      notify("请先在配置页完成全部必要验证。", "warning");
      setStep("config");
      return;
    }
    if (scan?.status === "RUNNING" || scan?.status === "PENDING") {
      notify("当前盘点仍在运行，请等待完成。", "info");
      setStep("scan");
      return;
    }
    setBusy("scan");
    try {
      const saved = (await persistProject()) ?? project;
      if (!saved) return;
      await api.startScan(saved.id);
      const result = await api.getScan(saved.id);
      setScan(result);
      setPreflight(undefined);
      setPlan(undefined);
      setStep("scan");
      notify("已开始只读盘点。页面会持续刷新进度，本地文件不会被修改。", "info");
    } catch (error) {
      showError(error);
    } finally {
      setBusy("");
    }
  };

  const refreshPreflight = async () => {
    if (!project) return;
    if (!scan?.summary.scan_complete) {
      notify("请先等待本地目录盘点完整结束。", "warning");
      setStep("scan");
      return;
    }
    setBusy("preflight");
    try {
      const result = await api.getPreflight(project.id);
      setPreflight(result);
      notify(result.writable ? "预检通过，可以生成差异计划。" : "预检完成，仍有阻断项。", result.writable ? "ok" : "warning");
    } catch (error) {
      showError(error);
    } finally {
      setBusy("");
    }
  };

  const buildPlan = async () => {
    if (!project) return;
    setBusy("plan");
    try {
      const result = await api.buildPlan(project.id);
      setPlan(result);
      setStep("plan");
      notify("差异计划已生成，尚未执行任何远端写入。", "info");
    } catch (error) {
      showError(error);
    } finally {
      setBusy("");
    }
  };

  const confirmPlan = async () => {
    if (!project) return;
    setBusy("confirm");
    try {
      const result = await api.buildPlan(project.id, true);
      setPlan(result);
      notify("计划已确认。迁移仍需单独点击开始。");
    } catch (error) {
      showError(error);
    } finally {
      setBusy("");
    }
  };

  const startRun = async () => {
    if (!project) return;
    setBusy("run-start");
    try {
      const result = await api.startRun(project.id);
      const currentRun = await api.getRun(result.run_id);
      setRun(currentRun);
      setProject({ ...project, last_run_id: result.run_id });
      setStep("run");
      notify("迁移已启动，可安全暂停或关闭页面。");
    } catch (error) {
      showError(error);
    } finally {
      setBusy("");
    }
  };

  const controlRun = async (action: "pause" | "resume" | "stop" | "retry") => {
    if (!run) return;
    setBusy(`run-${action}`);
    try {
      const next = await api.controlRun(run.id, action);
      setRun(next);
      notify(
        action === "pause"
          ? "迁移已暂停，当前断点已落库。"
          : action === "resume"
            ? "已从安全断点恢复。"
            : action === "retry"
              ? "失败项已进入重试队列。"
              : "迁移已停止。",
        action === "stop" ? "warning" : "ok"
      );
    } catch (error) {
      showError(error);
    } finally {
      setBusy("");
    }
  };

  const reconcile = async () => {
    if (!project) return;
    setBusy("reconcile");
    try {
      const result = await api.reconcile(project.id);
      notify(`远端对账完成：匹配 ${result.matched}，冲突 ${result.conflicts}。`, result.conflicts ? "warning" : "ok");
      const audit = (await api.getAudit(project.id)) as {
        events?: AuditEvent[];
        items?: RunItem[];
      };
      setEvents(audit.events ?? []);
      setRunItems(audit.items ?? []);
    } catch (error) {
      showError(error);
    } finally {
      setBusy("");
    }
  };

  const exportAudit = async (format: "csv" | "json") => {
    if (!project) return;
    setBusy(`export-${format}`);
    try {
      const file = await api.exportAudit(project.id, format);
      downloadBlob(file, `Folder2Feishu_${project.name}_${new Date().toISOString().slice(0, 10)}.${format}`);
      notify(`审计${format.toUpperCase()}已导出。`);
    } catch (error) {
      showError(error);
    } finally {
      setBusy("");
    }
  };

  const refreshCurrentPage = async () => {
    setBusy("page-refresh");
    try {
      const [health, currentSettings, currentAuth, projects] = await Promise.all([
        api.health(),
        api.getSettings(),
        api.getAuthStatus(),
        api.listProjects()
      ]);
      setVersion(health.version);
      setSettings(currentSettings);
      setAuth(currentAuth);
      const activeProject =
        projects.find((item) => item.id === project?.id) ?? projects[0];
      if (activeProject) {
        setProject(activeProject);
        setDraft({
          name: activeProject.name,
          source_root: activeProject.source_root,
          target_wiki_url: activeProject.target_wiki_url,
          create_wrapper: true,
          wrapper_name: activeProject.wrapper_name ?? ""
        });
        await loadProjectData(activeProject);
      } else {
        setProject(undefined);
        setStep("config");
      }
      notify("当前页面数据已刷新，所在步骤保持不变。");
    } catch (error) {
      showError(error);
    } finally {
      setBusy("");
    }
  };

  const configReady =
    Boolean(project) &&
    (Object.values(validation) as ValidationState[ValidationKey][]).every(
      (item) => item.status === "passed"
    );
  const scanActive =
    busy === "scan" || scan?.status === "RUNNING" || scan?.status === "PENDING";

  const stepStatus = (id: StepId): "done" | "active" | "pending" | "blocked" => {
    if (id === step) return "active";
    if (id === "config") return configReady ? "done" : "pending";
    if (id === "scan") return scan?.summary.scan_complete ? "done" : "pending";
    if (id === "preflight")
      return preflight?.writable ? "done" : preflight ? "blocked" : "pending";
    if (id === "plan") return plan?.confirmed ? "done" : "pending";
    if (id === "run") return run?.state === "COMPLETED" ? "done" : "pending";
    return "pending";
  };

  const stepEnabled = (id: StepId): boolean => {
    if (id === "config") return true;
    if (id === "scan") return configReady;
    if (id === "preflight") return configReady && Boolean(scan?.summary.scan_complete);
    if (id === "plan") return configReady && Boolean(preflight?.writable);
    if (id === "run") return configReady && Boolean(run || plan?.confirmed);
    return false;
  };

  const stepDisabledReason = (id: StepId): string | undefined => {
    if (stepEnabled(id)) return undefined;
    if (id === "scan") return "先在配置页完成全部必要验证";
    if (id === "preflight") return "先完成本地目录盘点";
    if (id === "plan") return "先通过权限、容量与写入能力预检";
    if (id === "run") return "先确认差异计划";
    return undefined;
  };

  const activeStep = steps.find((item) => item.id === step) ?? steps[0];
  const progress = run ? formatPercent(run.completed, run.total) : 0;
  const byteProgress = run ? formatPercent(run.bytes_completed, run.bytes_total) : 0;
  const blocking = preflight?.checks.filter((check) => check.blocking) ?? [];
  const filteredActions = useMemo(
    () =>
      plan?.actions.filter((action) => actionFilter === "ALL" || action.kind === actionFilter) ?? [],
    [actionFilter, plan]
  );
  const filteredRunItems = useMemo(
    () =>
      runItems.filter((item) => {
        if (runFilter === "FAILED")
          return item.status === "RETRYABLE" || item.status === "CONFLICT" || item.status === "MANUAL_ACTION";
        if (runFilter === "ACTIVE")
          return ["UPLOADING", "DRIVE_UPLOADED", "WIKI_MOVING", "VERIFYING"].includes(item.status);
        return true;
      }),
    [runFilter, runItems]
  );

  if (booting) {
    return (
      <main className="boot-screen" aria-busy="true">
        <div className="boot-screen__scope">
          <span className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <LoaderCircle className="spin" size={28} aria-hidden="true" />
          <h1>正在建立本机安全会话</h1>
          <p>读取迁移台账、授权状态与上次断点…</p>
        </div>
      </main>
    );
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <div>
            <span>FOLDER2FEISHU / WIKI</span>
            <strong>迁移作业台</strong>
          </div>
        </div>
        <div className="topbar__route" aria-label="迁移方向">
          <span><HardDrive size={15} /> WINDOWS LOCAL</span>
          <span className="route-line"><i /><ArrowRight size={15} /></span>
          <span className="route-destination"><Boxes size={15} /> FEISHU WIKI</span>
        </div>
        <div className="topbar__status">
          <button
            type="button"
            className="page-refresh"
            onClick={refreshCurrentPage}
            disabled={busy === "page-refresh"}
            aria-label="刷新当前页面数据"
            title="重新读取本机台账和飞书状态，不离开当前步骤"
          >
            <RefreshCcw
              className={busy === "page-refresh" ? "spin" : ""}
              size={15}
              aria-hidden="true"
            />
            <span>{busy === "page-refresh" ? "刷新中" : "刷新当前页"}</span>
          </button>
          {api.isDemo ? <span className="demo-flag">DEMO DATA</span> : null}
          <span className={`live-signal ${auth.authorized ? "is-live" : ""}`}>
            <i />
            {auth.authorized ? "身份已锁定" : "等待授权"}
          </span>
          <span className="version">BUILD {version}</span>
        </div>
      </header>

      <aside className="step-rail" aria-label="迁移步骤">
        <div className="step-rail__title">
          <span>RUNBOOK</span>
          <strong>五段式迁移</strong>
        </div>
        <nav>
          {steps.map((item) => {
            const Icon = item.icon;
            const status = stepStatus(item.id);
            return (
              <button
                type="button"
                key={item.id}
                className={`step-link is-${status}`}
                onClick={() => setStep(item.id)}
                disabled={!stepEnabled(item.id)}
                title={stepDisabledReason(item.id)}
                aria-current={step === item.id ? "step" : undefined}
              >
                <span className="step-link__number">{status === "done" ? <Check size={14} /> : item.no}</span>
                <span className="step-link__icon"><Icon size={18} /></span>
                <span className="step-link__copy">
                  <small>{item.eyebrow}</small>
                  <strong>{item.label}</strong>
                  <em>{item.description}</em>
                </span>
                {status === "blocked" ? <AlertTriangle size={15} className="step-warning" /> : null}
              </button>
            );
          })}
        </nav>
        <div className="rail-note">
          <LockKeyhole size={17} aria-hidden="true" />
          <div>
            <strong>只读源目录</strong>
            <span>本工具不会修改或删除 OneDrive 本地文件。</span>
          </div>
        </div>
      </aside>

      <main id="main" className="workspace">
        <div className="workspace-heading">
          <div>
            <span className="sequence">{activeStep.no} / 05 · {activeStep.eyebrow}</span>
            <h1>{activeStep.label}</h1>
            <p>{activeStep.description}</p>
          </div>
          {project ? (
            <div className="project-plate">
              <span>ACTIVE PROJECT</span>
              <strong>{project.name}</strong>
              <small>ID · {project.id}</small>
            </div>
          ) : (
            <div className="project-plate is-empty">
              <span>ACTIVE PROJECT</span>
              <strong>尚未创建迁移项目</strong>
              <small>完成配置验证后生成台账</small>
            </div>
          )}
        </div>

        {step === "config" ? (
          <div className="view-stack config-workbench">
            <Panel className="config-overview" tone={configReady ? "green" : "amber"}>
              <div className="config-overview__head">
                <div className="config-overview__title">
                  <span className="config-overview__mark"><Settings2 size={21} /></span>
                  <div>
                    <span className="eyebrow">MANDATORY CONFIG GATE</span>
                    <h2>先配置、逐项验证，再进入迁移流程</h2>
                    <p>全部必要配置逐项验证通过后，系统才会开放“盘点”及后续步骤。</p>
                  </div>
                </div>
                <div className="config-overview__actions">
                  <div className={`config-score ${configReady ? "is-ready" : ""}`}>
                    <strong>
                      {
                        (Object.values(validation) as ValidationState[ValidationKey][]).filter(
                          (item) => item.status === "passed"
                        ).length
                      }
                      <small>/ 7</small>
                    </strong>
                    <span>{configReady ? "READY" : "REQUIRED"}</span>
                  </div>
                  <Button
                    icon={SearchCheck}
                    busy={busy === "validate-all"}
                    onClick={() => validateAll()}
                  >
                    一键验证全部
                  </Button>
                  <Button
                    variant="primary"
                    icon={ArrowRight}
                    disabled={!configReady}
                    onClick={() => setStep("scan")}
                  >
                    进入盘点
                  </Button>
                </div>
              </div>
              <div className="validation-grid" aria-label="必要配置验证状态">
                {(
                  [
                    ["app", "01", "飞书应用", "App ID / Secret / 回调地址"],
                    ["oauth", "02", "OAuth 身份", "固定用户与完整权限范围"],
                    ["throttle", "03", "调用限额", "QPS / Wiki 频率 / 日预算"],
                    ["source", "04", "本地来源", "Windows 绝对路径"],
                    ["target", "05", "知识库落点", "Wiki 父节点 URL"],
                    ["policy", "06", "安全策略", "根节点与安全增量"]
                  ] as Array<[ValidationKey, string, string, string]>
                ).map(([key, no, label, detail]) => (
                  <article className={`validation-item is-${validation[key].status}`} key={key}>
                    <span>{no}</span>
                    <div>
                      <strong>{label}</strong>
                      <small>{detail}</small>
                      <p>{validation[key].message}</p>
                    </div>
                    <ValidationBadge {...validation[key]} />
                  </article>
                ))}
              </div>
              <div className="configuration-gap-note" role="note">
                <AlertTriangle size={17} />
                <div>
                  <strong>验证边界</strong>
                  <span>
                    本页每个按钮都调用独立后端验证：应用凭据、OAuth 身份、本地根层可读性和
                    Wiki 页面编辑权限均会真实检查。不会为验证而写入飞书；容器编辑能力由首个小批试迁确认，
                    OneDrive 占位状态与全目录完整性由“盘点”检查。
                  </span>
                </div>
              </div>
            </Panel>

            <div className="view-grid view-grid--connect">
              <Panel>
                <PanelHeading
                  eyebrow="APPLICATION CREDENTIALS"
                  title="飞书应用与本机安全配置"
                  copy="App Secret 只提交给本机后端，以 Windows DPAPI 加密保存，页面不会回显。"
                  icon={KeyRound}
                  tools={<ValidationBadge {...validation.app} />}
                />
                <form
                  className="form-stack"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void validateApp();
                  }}
                >
                  <div className="field-grid">
                    <Field label="飞书 App ID" required>
                      <input
                        value={settings.app_id}
                        onChange={(event) => {
                          setSettings({ ...settings, app_id: event.target.value });
                          markValidation("app", "idle", "应用配置已修改，请重新验证");
                          markValidation("oauth", "idle", "应用配置变化后需要重新验证 OAuth 身份");
                          setPreflight(undefined);
                          setPlan(undefined);
                        }}
                        placeholder="cli_xxxxxxxxxxxxxxxx"
                        autoComplete="off"
                        required
                      />
                    </Field>
                    <Field
                      label="App Secret"
                      hint={settings.app_secret_configured ? "已加密保存；留空表示不更换。" : "首次配置必须填写，页面不会回显。"}
                      required={!settings.app_secret_configured}
                    >
                      <input
                        type="password"
                        value={secret}
                        onChange={(event) => {
                          setSecret(event.target.value);
                          markValidation("app", "idle", "App Secret 已修改，请重新验证");
                          markValidation("oauth", "idle", "应用配置变化后需要重新验证 OAuth 身份");
                          setPreflight(undefined);
                          setPlan(undefined);
                        }}
                        placeholder={settings.app_secret_configured ? "••••••••••••••••" : "输入 App Secret"}
                        autoComplete="new-password"
                        required={!settings.app_secret_configured}
                      />
                    </Field>
                  </div>
                  <Field label="OAuth 回调地址" required hint="必须与飞书开放平台安全设置完全一致。">
                    <input
                      value={settings.redirect_uri}
                      onChange={(event) => {
                        setSettings({ ...settings, redirect_uri: event.target.value });
                        markValidation("app", "idle", "回调地址已修改，请重新验证");
                        markValidation("oauth", "idle", "应用配置变化后需要重新验证 OAuth 身份");
                        setPreflight(undefined);
                        setPlan(undefined);
                      }}
                      required
                    />
                  </Field>
                  <div className="scope-rack" aria-label="所需权限范围">
                    <span className="scope-rack__label">REQUIRED SCOPES</span>
                    {DEFAULT_SCOPES.map((scope) => (
                      <code key={scope}><Check size={12} /> {scope}</code>
                    ))}
                  </div>
                  <div className="button-row">
                    <Button
                      type="submit"
                      icon={ShieldCheck}
                      busy={validation.app.status === "checking"}
                    >
                      验证应用配置
                    </Button>
                    <Button
                      type="button"
                      variant="primary"
                      icon={ExternalLink}
                      busy={busy === "oauth"}
                      onClick={beginAuth}
                      disabled={validation.app.status !== "passed"}
                    >
                      前往飞书授权
                    </Button>
                  </div>
                </form>
              </Panel>

              <div className="side-stack">
                <Panel tone={auth.authorized ? "green" : "amber"}>
                  <div className="identity-card">
                    <div className="identity-card__top">
                      <div className="identity-card__seal">
                        {auth.authorized ? <BadgeCheck size={32} /> : <KeyRound size={30} />}
                      </div>
                      <ValidationBadge {...validation.oauth} />
                    </div>
                    <span className="eyebrow">FIXED OPERATOR IDENTITY</span>
                    <h3>{auth.authorized ? auth.user_name ?? "飞书用户已授权" : "等待完成飞书授权"}</h3>
                    <p>
                      {auth.authorized
                        ? "此身份将贯穿上传、中转、迁入知识库和远端回读。"
                        : "先验证应用配置，再在飞书页面确认所需权限。"}
                    </p>
                    <div className="identity-card__meta">
                      <span>状态 <b>{auth.authorized ? "READY" : "NOT READY"}</b></span>
                      <span>凭据 <b>WINDOWS DPAPI</b></span>
                    </div>
                    <Button
                      icon={SearchCheck}
                      busy={validation.oauth.status === "checking"}
                      onClick={validateOauth}
                    >
                      验证当前身份
                    </Button>
                  </div>
                </Panel>
                <Panel>
                  <div className="security-list">
                    <h3>本机安全边界</h3>
                    <div><LockKeyhole size={17} /><span><b>不在浏览器存 Token</b><small>OAuth Token 与 Secret 不进入 localStorage。</small></span></div>
                    <div><Server size={17} /><span><b>仅监听 127.0.0.1</b><small>迁移控制面不会暴露到局域网。</small></span></div>
                    <div><Database size={17} /><span><b>台账远离 OneDrive</b><small>数据库保存在 LOCALAPPDATA 并启用 WAL。</small></span></div>
                  </div>
                </Panel>
              </div>
            </div>

            <div className="config-operations-grid">
              <Panel>
                <PanelHeading
                  eyebrow="RATE & QUOTA ENVELOPE"
                  title="上传限流与每日调用预算"
                  copy="客户端主动低于飞书上限；服务端 429 或配额窗口仍会触发安全暂停。"
                  icon={Gauge}
                  tools={<ValidationBadge {...validation.throttle} />}
                />
                <div className="operation-config">
                  <div className="field-grid field-grid--three">
                    <Field label="文件上传 QPS" required hint="范围：大于 0 且不超过 4">
                      <input
                        type="number"
                        min="0.1"
                        max="4"
                        step="0.1"
                        value={settings.upload_qps}
                        onChange={(event) => {
                          setSettings({
                            ...settings,
                            upload_qps: Number(event.target.value || 0)
                          });
                          markValidation("throttle", "idle", "上传 QPS 已修改，请重新验证");
                          setPlan(undefined);
                        }}
                      />
                    </Field>
                    <Field label="Wiki 调用 / 分钟" required hint="范围：1–90">
                      <input
                        type="number"
                        min="1"
                        max="90"
                        step="1"
                        value={settings.wiki_calls_per_minute}
                        onChange={(event) => {
                          setSettings({
                            ...settings,
                            wiki_calls_per_minute: Number(event.target.value || 0)
                          });
                          markValidation("throttle", "idle", "Wiki 调用频率已修改，请重新验证");
                          setPlan(undefined);
                        }}
                      />
                    </Field>
                    <Field label="每日上传调用预算" required hint="范围：1–9500">
                      <input
                        type="number"
                        min="1"
                        max="9500"
                        step="1"
                        value={settings.daily_upload_budget}
                        onChange={(event) => {
                          setSettings({
                            ...settings,
                            daily_upload_budget: Number(event.target.value || 0)
                          });
                          markValidation("throttle", "idle", "每日预算已修改，请重新验证");
                          setPlan(undefined);
                        }}
                      />
                    </Field>
                  </div>
                  <div className="quota-rule">
                    <Gauge size={17} />
                    <span>
                      <strong>自动保护</strong>
                      达到约定日预算后暂停并保留断点，在下一配额窗口恢复。
                    </span>
                  </div>
                  <Button
                    icon={SearchCheck}
                    busy={validation.throttle.status === "checking"}
                    onClick={validateThrottle}
                  >
                    验证并保存调用限额
                  </Button>
                </div>
              </Panel>

            </div>

            <div className="view-grid view-grid--source config-second">
              <Panel className="route-builder">
                <PanelHeading
                  eyebrow="ONE-WAY MIGRATION ROUTE"
                  title="唯一来源、唯一落点与安全增量"
                  copy="源目录始终只读；飞书云盘只承担临时中转，最终节点进入知识库。"
                  icon={Route}
                />
                <form onSubmit={validateAll}>
                  <div className="route-node">
                    <div className="route-node__type">
                      <HardDrive size={21} />
                      <span>03 · SOURCE</span>
                    </div>
                    <div className="route-node__body">
                      <Field label="本地 OneDrive 已下载目录" required hint="请确保目标文件显示为绿色实心勾，不能是云端占位。">
                        <input
                          value={draft.source_root}
                          onChange={(event) => {
                            setDraft({ ...draft, source_root: event.target.value });
                            markValidation("source", "idle", "本地路径已修改，请重新验证");
                            setScan(undefined);
                            setPreflight(undefined);
                            setPlan(undefined);
                          }}
                          placeholder="D:\TechStyle\Team FabDazzle - 文档"
                          required
                        />
                      </Field>
                      <div className="inline-verify">
                        <ValidationBadge {...validation.source} />
                        <Button
                          type="button"
                          variant="ghost"
                          icon={SearchCheck}
                          busy={validation.source.status === "checking"}
                          onClick={validateSource}
                        >
                          验证本地目录配置
                        </Button>
                      </div>
                    </div>
                    <span className="route-node__mode">READ ONLY</span>
                  </div>
                  <div className="route-spine" aria-hidden="true">
                    <i />
                    <ArrowRight size={18} />
                    <span>安全上传通道</span>
                  </div>
                  <div className="route-node route-node--target">
                    <div className="route-node__type">
                      <Boxes size={21} />
                      <span>04 · DESTINATION</span>
                    </div>
                    <div className="route-node__body">
                      <Field label="飞书知识库父节点 URL" required hint="支持 /wiki/… 地址；必须拥有此节点的容器编辑权限。">
                        <input
                          type="url"
                          value={draft.target_wiki_url}
                          onChange={(event) => {
                            setDraft({ ...draft, target_wiki_url: event.target.value });
                            markValidation("target", "idle", "知识库地址已修改，请重新验证");
                            setPreflight(undefined);
                            setPlan(undefined);
                          }}
                          placeholder="https://example.feishu.cn/wiki/xxxxxxxx"
                          required
                        />
                      </Field>
                      <div className="inline-verify">
                        <ValidationBadge {...validation.target} />
                        <Button
                          type="button"
                          variant="ghost"
                          icon={SearchCheck}
                          busy={validation.target.status === "checking"}
                          onClick={validateTarget}
                        >
                          验证知识库地址
                        </Button>
                      </div>
                    </div>
                    <span className="route-node__mode">FINAL</span>
                  </div>
                  <div className="field-grid route-options">
                    <Field label="迁移项目名称">
                      <input
                        value={draft.name}
                        onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                      />
                    </Field>
                    <Field label="知识库根节点名称" hint="默认使用本地根目录名称。">
                      <input
                        value={draft.wrapper_name ?? ""}
                        onChange={(event) => {
                          setDraft({ ...draft, wrapper_name: event.target.value });
                          markValidation("policy", "idle", "根节点名称已修改，请重新验证");
                          setPlan(undefined);
                        }}
                        placeholder="Team FabDazzle - 文档"
                        disabled={!draft.create_wrapper}
                      />
                    </Field>
                  </div>
                  <Toggle
                    checked={draft.create_wrapper}
                    onChange={(value) => {
                      setDraft({ ...draft, create_wrapper: value });
                      markValidation("policy", "idle", "根节点策略已修改，请重新验证");
                      setPlan(undefined);
                    }}
                    label="创建同名根节点"
                    description="必需。完整保留本地根目录，并避免不同来源混入同一知识库层级。"
                  />
                  <div className="policy-verify">
                    <div>
                      <ShieldCheck size={19} />
                      <span>
                        <strong>安全增量策略</strong>
                        <small>内容变更留历史、本地删除只报告、远端人工变化转冲突。</small>
                      </span>
                    </div>
                    <ValidationBadge {...validation.policy} />
                    <Button
                      type="button"
                      variant="ghost"
                      icon={SearchCheck}
                      busy={validation.policy.status === "checking"}
                      onClick={validatePolicy}
                    >
                      验证安全策略
                    </Button>
                  </div>
                  <div className="button-row button-row--end">
                    <Button type="submit" icon={SearchCheck} busy={busy === "validate-all"}>
                      一键验证全部
                    </Button>
                    <Button
                      type="button"
                      variant="primary"
                      icon={ArrowRight}
                      onClick={() => setStep("scan")}
                      disabled={!configReady}
                    >
                      验证完成，进入盘点
                    </Button>
                  </div>
                </form>
              </Panel>

              <div className="side-stack">
                <Panel>
                  <div className="flow-legend">
                    <span className="eyebrow">TRANSFER PHYSICS</span>
                    <h3>文件会经过哪里？</h3>
                    <div className="flow-legend__line">
                      <span><HardDrive size={17} /> 本地文件</span>
                      <ArrowRight size={14} />
                      <span><Cloud size={17} /> 专用中转</span>
                      <ArrowRight size={14} />
                      <span><Boxes size={17} /> 知识库</span>
                    </div>
                    <p>成功迁入后，中转目录不再保留该文件。Word、Excel、PPTX、PDF 保持原格式。</p>
                  </div>
                </Panel>
                <Panel tone="green">
                  <div className="safety-contract">
                    <ShieldCheck size={24} />
                    <div>
                      <span className="eyebrow">SAFE INCREMENTAL</span>
                      <h3>安全增量约定</h3>
                    </div>
                    <ul>
                      <li>内容未变：不重复上传</li>
                      <li>仅移动改名：复用原 Wiki 节点</li>
                      <li>内容变化：旧版进入历史区</li>
                      <li>本地删除：只报告，不删飞书</li>
                    </ul>
                  </div>
                </Panel>
              </div>
            </div>
          </div>
        ) : null}

        {step === "scan" ? (
          <div className="view-stack">
            <Panel className="inventory-command" tone={scan?.summary.scan_complete ? "green" : ""}>
              <div className="inventory-command__route">
                <div>
                  <span className="eyebrow">READ-ONLY INVENTORY</span>
                  <h2>真实读取本地目录，建立可恢复迁移台账</h2>
                  <p>
                    此阶段递归读取目录、File ID、大小、时间与 SHA-256，不修改或删除任何本地文件。
                  </p>
                </div>
                <div className="inventory-route">
                  <span><HardDrive size={16} /> {draft.source_root || "未配置本地目录"}</span>
                  <ArrowRight size={16} />
                  <span><Database size={16} /> SQLite 台账</span>
                </div>
              </div>
              <div className="inventory-command__actions">
                <div className={`scan-state is-${(scan?.status ?? "IDLE").toLowerCase()}`}>
                  {scanActive ? (
                    <LoaderCircle className="spin" size={18} />
                  ) : scan?.summary.scan_complete ? (
                    <CheckCircle2 size={18} />
                  ) : (
                    <ScanLine size={18} />
                  )}
                  <span>
                    <small>INVENTORY STATE</small>
                    <strong>
                      {scanActive
                        ? "扫描进行中"
                        : scan?.summary.scan_complete
                          ? "盘点完整"
                          : scan?.status === "FAILED"
                            ? "盘点失败"
                            : "等待开始"}
                    </strong>
                  </span>
                </div>
                <Button
                  icon={scan ? RefreshCcw : ScanLine}
                  busy={scanActive}
                  onClick={startScan}
                  disabled={!configReady || scanActive}
                >
                  {scanActive ? "盘点进行中" : scan ? "重新只读盘点" : "开始只读盘点"}
                </Button>
                <Button
                  variant="primary"
                  icon={ArrowRight}
                  disabled={!scan?.summary.scan_complete}
                  onClick={() => setStep("preflight")}
                >
                  进入权限与容量预检
                </Button>
              </div>
            </Panel>

            {scan ? (
              <>
                <div className="metric-grid">
                  <Metric icon={File} label="文件" value={scan.summary.files.toLocaleString()} />
                  <Metric icon={Folder} label="目录" value={scan.summary.folders.toLocaleString()} />
                  <Metric
                    icon={HardDrive}
                    label="数据量"
                    value={formatBytes(scan.summary.bytes)}
                    note={
                      scan.summary.hashes_reused
                        ? `已复用 ${scan.summary.hashes_reused.toLocaleString()} 个文件哈希`
                        : scanActive
                          ? "首次盘点采用 4 路并发读取"
                          : "文件内容已完成校验"
                    }
                  />
                  <Metric
                    icon={Cloud}
                    label="OneDrive 占位"
                    value={scan.summary.placeholders.toLocaleString()}
                    note={scan.summary.placeholders ? "占位文件会阻断迁移" : "本地内容可继续检查"}
                    tone={scan.summary.placeholders ? "red" : "green"}
                  />
                </div>
                <div className="inventory-layout">
                  <Panel>
                    <PanelHeading
                      eyebrow="LOCAL DIRECTORY TREE"
                      title="原目录盘点结果"
                      copy="文件夹是一等对象；空目录也会在知识库中创建对应节点。"
                      icon={FolderTree}
                    />
                    <div className="tree-view inventory-tree">
                      {(tree.length ? tree : scan.tree).length ? (
                        (tree.length ? tree : scan.tree).map((node) => (
                          <TreeBranch key={node.id} node={node} />
                        ))
                      ) : (
                        <EmptyState
                          icon={FolderTree}
                          title="正在生成目录预览"
                          copy="大型目录会分批写入台账，扫描完成后显示抽样树。"
                        />
                      )}
                    </div>
                    <div className="tree-limit">
                      <span>MAX DEPTH <b>{scan.summary.max_depth}</b> / 50</span>
                      <span>MAX SIBLINGS <b>{scan.summary.max_siblings}</b> / 2,000</span>
                    </div>
                  </Panel>
                  <div className="side-stack">
                    <Panel tone={scan.checks.some((check) => check.blocking) ? "red" : "green"}>
                      <PanelHeading
                        eyebrow="INVENTORY EVIDENCE"
                        title="本地完整性检查"
                        copy="占位、不可读或超限对象会阻断；飞书不支持的 0 字节文件将记录后自动跳过。"
                        icon={SearchCheck}
                      />
                      <div className="inventory-findings">
                        <div><span>不可读</span><strong>{scan.summary.unreadable.toLocaleString()}</strong></div>
                        <div><span>0 字节</span><strong>{scan.summary.empty_files.toLocaleString()}</strong></div>
                        <div><span>名称过长</span><strong>{scan.summary.too_long_names.toLocaleString()}</strong></div>
                        <div><span>预计上传 API 调用</span><strong>{scan.summary.upload_calls.toLocaleString()}</strong></div>
                      </div>
                      <div className="finding-list">
                        {scan.checks.length ? (
                          scan.checks.map((check) => {
                            const Icon = severityIcon[check.severity];
                            return (
                              <article className={`finding is-${check.severity}`} key={check.code}>
                                <Icon size={16} />
                                <div>
                                  <strong>{check.title}</strong>
                                  <span>{check.message}</span>
                                </div>
                                {check.count ? <b>{check.count}</b> : null}
                              </article>
                            );
                          })
                        ) : (
                          <p className="inventory-clear"><CheckCircle2 size={17} /> 暂未发现本地盘点问题。</p>
                        )}
                      </div>
                    </Panel>
                    <Panel>
                      <div className="inventory-boundary">
                        <ShieldCheck size={22} />
                        <div>
                          <span className="eyebrow">VALIDATION HANDOFF</span>
                          <h3>本页确认本地事实</h3>
                          <p>
                            盘点完整后，下一步才会用固定 OAuth 身份真实检查 Wiki 父节点、
                            容器编辑权限、云盘根目录与租户容量。
                          </p>
                        </div>
                      </div>
                    </Panel>
                  </div>
                </div>
              </>
            ) : (
              <Panel>
                <EmptyState
                  icon={ScanLine}
                  title="尚未建立本地目录台账"
                  copy="配置页已只读验证目录存在且根层可枚举；点击“开始只读盘点”后，系统会进一步检查全部内容和 OneDrive 占位状态。"
                  action={
                    <Button
                      variant="primary"
                      icon={ScanLine}
                      busy={scanActive}
                      onClick={startScan}
                      disabled={!configReady || scanActive}
                    >
                      {scanActive ? "盘点进行中" : "开始只读盘点"}
                    </Button>
                  }
                />
              </Panel>
            )}
          </div>
        ) : null}

        {step === "preflight" ? (
          <div className="view-stack">
            {scan ? (
              <>
                <div className="metric-grid">
                  <Metric icon={File} label="文件" value={scan.summary.files.toLocaleString()} />
                  <Metric icon={Folder} label="目录" value={scan.summary.folders.toLocaleString()} />
                  <Metric icon={HardDrive} label="数据量" value={formatBytes(scan.summary.bytes)} />
                  <Metric
                    icon={Cloud}
                    label="预计上传 API 调用"
                    value={scan.summary.upload_calls.toLocaleString()}
                    note={`大文件分片会多次调用 · 约 ${scan.summary.estimated_days} 个自然日`}
                    tone="amber"
                  />
                </div>
                {blocking.length ? (
                  <div className="blocker-banner" role="alert">
                    <AlertTriangle size={22} />
                    <div>
                      <strong>{blocking.length} 类问题阻止正式迁移</strong>
                      <span>处理后点击“重新执行预检”；工具不会绕过 OneDrive 占位或权限问题。</span>
                    </div>
                    <Button icon={RefreshCcw} busy={busy === "preflight"} onClick={refreshPreflight}>
                      重新执行预检
                    </Button>
                  </div>
                ) : (
                  <div className="ready-banner">
                    <BadgeCheck size={22} />
                    <div>
                      <strong>预检通过，可以生成差异计划</strong>
                      <span>生成计划仍不会对飞书执行写入。</span>
                    </div>
                    <Button variant="primary" icon={Waypoints} busy={busy === "plan"} onClick={buildPlan}>
                      生成差异计划
                    </Button>
                  </div>
                )}
                <div className="preflight-layout">
                  <Panel>
                    <PanelHeading
                      eyebrow="GUARD RAIL MATRIX"
                      title="预检矩阵"
                      copy="阻断项必须清零；警告项会说明自动处理方式或需要关注的边界。"
                      icon={ShieldCheck}
                    />
                    <div className="check-grid">
                      {(preflight?.checks ?? scan.checks).map((check) => {
                        const Icon = severityIcon[check.severity];
                        return (
                          <article className={`check-card is-${check.severity}`} key={check.code}>
                            <span className="check-card__icon"><Icon size={19} /></span>
                            <div>
                              <span className="check-card__code">{check.code}</span>
                              <h3>{check.title}</h3>
                              <p>{check.message}</p>
                            </div>
                            {check.count ? <b>{check.count}</b> : <Check size={17} />}
                          </article>
                        );
                      })}
                    </div>
                  </Panel>
                  <Panel className="tree-panel">
                    <PanelHeading
                      eyebrow="LOCAL INVENTORY"
                      title="目录抽样"
                      copy="目录是独立迁移对象，空目录也会保留。"
                      icon={FolderTree}
                    />
                    <div className="tree-view">
                      {(tree.length ? tree : scan.tree).map((node) => (
                        <TreeBranch key={node.id} node={node} />
                      ))}
                    </div>
                    <div className="tree-limit">
                      <span>MAX DEPTH <b>{scan.summary.max_depth}</b> / 50</span>
                      <span>MAX SIBLINGS <b>{scan.summary.max_siblings}</b> / 2,000</span>
                    </div>
                  </Panel>
                </div>
              </>
            ) : (
              <Panel>
                <EmptyState
                  icon={ScanLine}
                  title="尚未盘点本地目录"
                  copy="先在“盘点”中完成本地目录的真实只读扫描。"
                  action={<Button icon={ArrowRight} onClick={() => setStep("scan")}>前往盘点</Button>}
                />
              </Panel>
            )}
          </div>
        ) : null}

        {step === "plan" ? (
          plan ? (
            <div className="view-stack">
              <div className="plan-header">
                <div>
                  <span className="eyebrow">IMMUTABLE WRITE PROPOSAL · {plan.id}</span>
                  <h2>每一项远端动作，先看清再执行</h2>
                  <p>生成于 {new Date(plan.created_at).toLocaleString("zh-CN")} · 预计跨 {plan.estimated_days} 个自然日</p>
                </div>
                <div className={`confirmation-seal ${plan.confirmed ? "is-confirmed" : ""}`}>
                  {plan.confirmed ? <BadgeCheck size={22} /> : <FileClock size={22} />}
                  <span>{plan.confirmed ? "PLAN CONFIRMED" : "AWAITING CONFIRMATION"}</span>
                </div>
              </div>
              <div className="action-grid">
                {plan.counts.map((item) => (
                  <button
                    type="button"
                    className={`action-counter tone-${actionTone[item.kind]} ${actionFilter === item.kind ? "is-selected" : ""}`}
                    key={item.kind}
                    onClick={() => setActionFilter(actionFilter === item.kind ? "ALL" : item.kind)}
                  >
                    <span>{actionLabel[item.kind]}</span>
                    <strong>{item.count.toLocaleString()}</strong>
                    <small>{item.kind}</small>
                  </button>
                ))}
              </div>
              <div className="plan-layout">
                <Panel>
                  <PanelHeading
                    eyebrow="ACTION LEDGER"
                    title={actionFilter === "ALL" ? "差异动作样本" : actionLabel[actionFilter]}
                    copy={`显示 ${filteredActions.length} 项代表性记录；完整台账可在运行页导出。`}
                    icon={ListFilter}
                    tools={
                      actionFilter !== "ALL" ? (
                        <Button variant="ghost" onClick={() => setActionFilter("ALL")}>清除筛选</Button>
                      ) : undefined
                    }
                  />
                  <div className="data-table" role="table" aria-label="差异动作">
                    <div className="data-table__head" role="row">
                      <span role="columnheader">动作</span>
                      <span role="columnheader">相对路径</span>
                      <span role="columnheader">判定依据</span>
                      <span role="columnheader">数据量</span>
                    </div>
                    {filteredActions.map((action) => (
                      <div className="data-table__row" role="row" key={action.id}>
                        <span role="cell"><i className={`action-pill tone-${actionTone[action.kind]}`}>{actionLabel[action.kind]}</i></span>
                        <span role="cell" className="path-cell" title={action.relative_path}>{action.relative_path}</span>
                        <span role="cell">{action.reason}</span>
                        <span role="cell">{action.bytes ? formatBytes(action.bytes) : "—"}</span>
                      </div>
                    ))}
                  </div>
                </Panel>
                <div className="side-stack">
                  <Panel tone="green">
                    <div className="incremental-map">
                      <span className="eyebrow">DECISION RULES</span>
                      <h3>安全增量决策链</h3>
                      <div><span>01</span><b>路径 + SHA 未变</b><em>跳过</em></div>
                      <div><span>02</span><b>File ID 未变</b><em>移动 / 改名</em></div>
                      <div><span>03</span><b>内容发生变化</b><em>安全换版</em></div>
                      <div><span>04</span><b>本地文件缺失</b><em>仅报告</em></div>
                      <div><span>05</span><b>远端人工变化</b><em>冲突停止</em></div>
                    </div>
                  </Panel>
                  <Panel tone={plan.confirmed ? "green" : "amber"}>
                    <div className="launch-card">
                      <ShieldCheck size={25} />
                      <h3>{plan.confirmed ? "计划已锁定" : "确认后才能迁移"}</h3>
                      <p>
                        {plan.confirmed
                          ? `共 ${plan.writable_actions.toLocaleString()} 项写操作，可随时暂停并从断点恢复。`
                          : "确认只锁定当前盘点快照，不会立即上传。"}
                      </p>
                      {!plan.confirmed ? (
                        <Button variant="primary" icon={Check} busy={busy === "confirm"} onClick={confirmPlan}>
                          最终确认当前计划
                        </Button>
                      ) : (
                        <Button
                          variant="primary"
                          icon={Play}
                          busy={busy === "run-start"}
                          onClick={startRun}
                          disabled={Boolean(blocking.length)}
                        >
                          开始迁移到知识库
                        </Button>
                      )}
                      {blocking.length ? <small className="launch-blocked">仍有预检阻断项，无法启动。</small> : null}
                    </div>
                  </Panel>
                </div>
              </div>
            </div>
          ) : (
            <Panel>
              <EmptyState
                icon={Waypoints}
                title="尚未生成差异计划"
                copy="先完成本地盘点和预检，系统才会列出准备写入飞书的每一项动作。"
                action={
                  <Button
                    icon={preflight?.writable ? Waypoints : ArrowRight}
                    onClick={preflight?.writable ? buildPlan : () => setStep("preflight")}
                    busy={busy === "plan"}
                  >
                    {preflight?.writable ? "现在生成计划" : "返回预检"}
                  </Button>
                }
              />
            </Panel>
          )
        ) : null}

        {step === "run" ? (
          run ? (
            <div className="view-stack">
              <div className={`run-marquee is-${run.state.toLowerCase()}`}>
                <div className="run-marquee__pulse"><i /><span /></div>
                <div>
                  <span className="eyebrow">RUN · {run.id}</span>
                  <h2>{statusLabel[run.state] ?? run.state}</h2>
                  <p>{run.current_path ?? "当前没有正在处理的文件"}</p>
                </div>
                <div className="run-marquee__controls">
                  {run.state === "RUNNING" ? (
                    <Button icon={CirclePause} busy={busy === "run-pause"} onClick={() => controlRun("pause")}>安全暂停</Button>
                  ) : (
                    <Button variant="primary" icon={CirclePlay} busy={busy === "run-resume"} onClick={() => controlRun("resume")}>断点恢复</Button>
                  )}
                  <Button icon={RotateCcw} busy={busy === "run-retry"} onClick={() => controlRun("retry")}>重试失败项</Button>
                  <Button variant="danger" icon={Square} busy={busy === "run-stop"} onClick={() => controlRun("stop")}>停止</Button>
                </div>
              </div>
              <div className="run-metrics">
                <Panel className="progress-panel">
                  <div className="progress-panel__top">
                    <div>
                      <span className="eyebrow">ITEM PROGRESS</span>
                      <strong>{progress}<small>%</small></strong>
                    </div>
                    <div className="progress-copy">
                      <b>{run.completed.toLocaleString()} / {run.total.toLocaleString()}</b>
                      <span>失败 {run.failed} · 冲突 {run.conflicts}</span>
                    </div>
                  </div>
                  <div className="progress-track"><i style={{ width: `${progress}%` }} /></div>
                  <div className="progress-panel__bottom">
                    <span>数据进度 {byteProgress}% · {formatBytes(run.bytes_completed)} / {formatBytes(run.bytes_total)}</span>
                    <span>预计剩余 <b>{formatEta(run.eta_seconds)}</b></span>
                  </div>
                </Panel>
                <Panel className="quota-panel">
                  <span className="eyebrow">DAILY UPLOAD BUDGET</span>
                  <div className="quota-dial" style={{ "--quota": `${formatPercent(run.quota.upload_calls_used, run.quota.upload_calls_limit)}%` } as React.CSSProperties}>
                    <span><strong>{run.quota.upload_calls_used.toLocaleString()}</strong><small>/ {run.quota.upload_calls_limit.toLocaleString()}</small></span>
                  </div>
                  <div className="quota-panel__copy">
                    <span>Wiki 节流 <b>{run.quota.wiki_calls_minute} / {run.quota.wiki_calls_limit}</b> 次/分钟</span>
                    <span>配额耗尽时自动暂停，不丢断点</span>
                  </div>
                </Panel>
              </div>
              <div className="run-layout">
                <Panel>
                  <PanelHeading
                    eyebrow="ITEM QUEUE"
                    title="文件执行队列"
                    copy="上传 token、分片进度和 Wiki 任务号会在每一步立即落库。"
                    icon={UploadCloud}
                    tools={
                      <div className="segmented">
                        {(["ALL", "ACTIVE", "FAILED"] as const).map((filter) => (
                          <button
                            type="button"
                            key={filter}
                            className={runFilter === filter ? "is-active" : ""}
                            onClick={() => setRunFilter(filter)}
                          >
                            {filter === "ALL" ? "全部" : filter === "ACTIVE" ? "处理中" : "失败 / 冲突"}
                          </button>
                        ))}
                      </div>
                    }
                  />
                  {filteredRunItems.length ? (
                    <div className="queue-list">
                      {filteredRunItems.map((item) => (
                        <article className={`queue-item is-${item.status.toLowerCase()}`} key={item.id}>
                          <span className="queue-item__status">
                            {item.status === "DONE" ? <CheckCircle2 /> : item.status === "UPLOADING" ? <LoaderCircle className="spin" /> : <FileWarning />}
                          </span>
                          <div className="queue-item__path">
                            <strong>{item.relative_path.split("\\").at(-1)}</strong>
                            <span>{item.relative_path}</span>
                            {item.error_message ? <small>{item.error_code} · {item.error_message}</small> : null}
                          </div>
                          <div className="queue-item__progress">
                            <span>{statusLabel[item.status] ?? item.status}</span>
                            <div><i style={{ width: `${item.progress}%` }} /></div>
                          </div>
                          <b>{item.progress}%</b>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <EmptyState icon={FileCheck2} title="此筛选下没有项目" copy="切换筛选条件查看其他执行记录。" />
                  )}
                </Panel>
                <div className="side-stack">
                  <Panel>
                    <PanelHeading eyebrow="REMOTE EVIDENCE" title="对账与审计" icon={SearchCheck} />
                    <div className="audit-tools">
                      <Button icon={RefreshCcw} busy={busy === "reconcile"} onClick={reconcile}>立即远端对账</Button>
                      <div>
                        <Button variant="ghost" icon={Download} busy={busy === "export-csv"} onClick={() => exportAudit("csv")}>CSV</Button>
                        <Button variant="ghost" icon={Download} busy={busy === "export-json"} onClick={() => exportAudit("json")}>JSON</Button>
                      </div>
                    </div>
                    <div className="timeline">
                      {events.length ? events.map((event) => (
                        <article className={`timeline__event is-${event.level.toLowerCase()}`} key={event.id}>
                          <i />
                          <time>{new Date(event.occurred_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
                          <div>
                            <span>{event.stage}</span>
                            <strong>{event.message}</strong>
                            {event.relative_path ? <small>{event.relative_path}</small> : null}
                            {event.evidence ? <code>{event.evidence}</code> : null}
                          </div>
                        </article>
                      )) : <p className="muted">暂无审计事件。</p>}
                    </div>
                  </Panel>
                </div>
              </div>
            </div>
          ) : (
            <Panel>
              <EmptyState
                icon={PanelTop}
                title="尚无迁移运行"
                copy="确认差异计划后，运行页会展示实时进度、配额、失败队列和远端证据。"
                action={<Button icon={ArrowRight} onClick={() => setStep("plan")}>返回差异计划</Button>}
              />
            </Panel>
          )
        ) : null}
      </main>

      <footer className="statusbar">
        <span><i className={auth.authorized ? "ok" : "warning"} /> OAuth {auth.authorized ? "READY" : "WAITING"}</span>
        <span><i className={project ? "ok" : ""} /> SQLITE WAL</span>
        <span><i className="ok" /> LOCALHOST ONLY</span>
        <span className="statusbar__right">源目录保护 · READ ONLY</span>
      </footer>

      {notice ? (
        <div className={`toast is-${notice.tone}`} role="status" aria-live="polite">
          {notice.tone === "error" ? <OctagonX /> : notice.tone === "warning" ? <AlertTriangle /> : <CheckCircle2 />}
          <span>{notice.text}</span>
          <button type="button" aria-label="关闭提示" onClick={() => setNotice(undefined)}>×</button>
        </div>
      ) : null}
    </div>
  );
}

export default App;
