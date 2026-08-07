import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
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
  RuntimeLogEntry,
  ScanResult,
  Severity,
  StepId,
  TreeNode
} from "../types";
import {
  ValidationKey,
  ValidationState,
  ValidationStatus,
  emptyDraft,
  emptySettings,
  emptyValidation,
  isValidDriveFolderUrl,
  isValidSourceRoot,
  updateTreeNode,
  withBootTimeout
} from "../lib/defaults";
import { DEFAULT_SCOPES, translateTechnicalMessage } from "../lib/labels";
import { readSavedStep, rememberStep } from "../lib/steps";
import { downloadBlob, formatPercent } from "../utils";
import { usePolling } from "./usePolling";

const ACTIVE_ITEM_STATUSES = ["UPLOADING", "DRIVE_UPLOADED", "WIKI_MOVING", "VERIFYING"];
const ATTENTION_ITEM_STATUSES = ["RETRYABLE", "CONFLICT", "MANUAL_ACTION"];
const RUNTIME_LOG_WINDOW = 160;

export type Notice = { tone: Severity; text: string };
export type RunFilter = "ALL" | "FAILED" | "ACTIVE";
export type StepStatus = "done" | "active" | "pending" | "blocked";

export function useMigrationConsole() {
  const [step, setStep] = useState<StepId>(readSavedStep);
  const [version, setVersion] = useState("3.0");
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
  const [backgroundTask, setBackgroundTask] = useState<RunSummary>();
  const [runItems, setRunItems] = useState<RunItem[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [runtimeLogs, setRuntimeLogs] = useState<RuntimeLogEntry[]>([]);
  const runtimeLogCursor = useRef<number>();
  const [validation, setValidation] = useState<ValidationState>(emptyValidation);
  const [actionFilter, setActionFilter] = useState<PlannedActionKind | "ALL">("ALL");
  const [runFilter, setRunFilter] = useState<RunFilter>("ALL");
  const [busy, setBusy] = useState("");
  const [booting, setBooting] = useState(true);
  const [bootError, setBootError] = useState("");
  const [notice, setNotice] = useState<Notice>();

  const notify = useCallback((text: string, tone: Severity = "ok") => {
    setNotice({ text, tone });
    window.setTimeout(() => setNotice(undefined), 4200);
  }, []);

  const showError = useCallback(
    (error: unknown) => {
      const message =
        error instanceof ApiError || error instanceof Error
          ? error.message
          : "操作没有完成，请重试。";
      notify(translateTechnicalMessage(message), "error");
    },
    [notify]
  );

  const markValidation = useCallback(
    (key: ValidationKey, status: ValidationStatus, message: string) => {
      setValidation((current) => ({ ...current, [key]: { status, message } }));
    },
    []
  );

  /** Any edit that invalidates downstream evidence must clear it. */
  const invalidateDownstream = useCallback((from: "config" | "source" | "target") => {
    if (from === "config" || from === "source") setScan(undefined);
    setPreflight(undefined);
    setPlan(undefined);
  }, []);

  const loadProjectData = useCallback(async (activeProject: Project) => {
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
      activeProject.last_run_id ? api.getRun(activeProject.last_run_id) : Promise.resolve(undefined),
      api.getProjectTasks
        ? api.getProjectTasks(activeProject.id)
        : Promise.resolve([] as RunSummary[])
    ]);
    if (calls[0].status === "fulfilled" && calls[0].value) setPreflight(calls[0].value);
    if (calls[1].status === "fulfilled") {
      const roots = calls[1].value;
      if (roots.length === 1 && roots[0].kind === "folder" && roots[0].child_count) {
        const children = await api.getTree(activeProject.id, roots[0].relative_path).catch(() => []);
        setTree([{ ...roots[0], children }]);
      } else {
        setTree(roots);
      }
    }
    if (calls[2].status === "fulfilled") setPlan(calls[2].value);
    if (calls[3].status === "fulfilled") {
      const audit = calls[3].value as { events?: AuditEvent[]; items?: RunItem[] };
      setEvents(audit.events ?? []);
      setRunItems(audit.items ?? []);
    }
    if (calls[4].status === "fulfilled" && calls[4].value) setRun(calls[4].value);
    if (calls[5].status === "fulfilled") {
      const active = calls[5].value.find(
        (task) =>
          task.state === "RUNNING" && (task.kind === "PLAN" || task.kind === "RECONCILIATION")
      );
      if (active) setBackgroundTask(active);
      const latestMigration = calls[5].value.find((task) => task.kind === "MIGRATION");
      if (latestMigration) setRun(latestMigration);
    }
  }, []);

  useEffect(() => rememberStep(step), [step]);

  // ---------------------------------------------------------------- boot
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        await withBootTimeout(api.getSession());
        const [health, currentSettings, currentAuth, projects] = await withBootTimeout(
          Promise.all([api.health(), api.getSettings(), api.getAuthStatus(), api.listProjects()])
        );
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
                : "请填写应用编号、应用密钥和回调地址"
          },
          oauth: {
            status: "idle",
            message: currentAuth.authorized
              ? "已发现用户授权，请点击按钮回读身份与五项权限"
              : "请完成飞书用户授权"
          },
          throttle: {
            status:
              currentSettings.upload_qps > 0 && currentSettings.upload_qps <= 5 ? "passed" : "idle",
            message: "云盘上传速率已加载；触发飞书限流时会自动退避"
          },
          source: {
            status: "idle",
            message: activeProject?.source_root
              ? "本地根目录已保存，请点击按钮做只读可读性检查"
              : "请填写本机绝对路径"
          },
          target: {
            status: "idle",
            message: activeProject?.target_wiki_url
              ? "云盘目标文件夹已保存，请点击按钮检查访问与写入权限"
              : "请填写飞书云盘文件夹地址"
          },
          policy: {
            status: activeProject ? "passed" : "idle",
            message: activeProject ? "安全增量与同名根节点策略已固定" : "请确认安全增量选项"
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
          // A large inventory tree, audit ledger or live preflight can take a
          // long time to hydrate. They must never hold the whole application
          // behind the boot screen.
          void loadProjectData(activeProject);
        } else {
          setStep("config");
        }
      } catch (error) {
        if (alive) {
          setBootError(
            error instanceof ApiError || error instanceof Error
              ? error.message
              : "本机服务初始化失败。"
          );
        }
      } finally {
        if (alive) setBooting(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [loadProjectData]);

  // ------------------------------------------------------------- polling
  usePolling(
    async () => {
      if (!run) return;
      setRun(await api.getRun(run.id));
    },
    run?.state === "RUNNING",
    { key: run?.id ?? "" }
  );

  useEffect(() => {
    // A new run starts a fresh log window rather than appending to the old one.
    runtimeLogCursor.current = undefined;
    setRuntimeLogs([]);
  }, [run?.id]);

  usePolling(
    async () => {
      const response = await api.getRuntimeLogs(runtimeLogCursor.current);
      runtimeLogCursor.current = response.next_after;
      setRuntimeLogs((current) => {
        const next = response.reset ? response.entries : [...current, ...response.entries];
        return Array.from(new Map(next.map((entry) => [entry.id, entry])).values()).slice(
          -RUNTIME_LOG_WINDOW
        );
      });
    },
    Boolean(run) && run?.state === "RUNNING",
    { key: run?.id ?? "" }
  );

  usePolling(
    async () => {
      if (!backgroundTask || !project) return;
      const next = await api.getRun(backgroundTask.id);
      setBackgroundTask(next);
      if (next.state === "COMPLETED") {
        if (next.kind === "PLAN") {
          setPlan(await api.getPlan(project.id));
          setStep("plan");
          notify("差异计划已生成，尚未执行任何远端写入。", "info");
        } else if (next.kind === "RECONCILIATION") {
          const audit = await api.getAudit(project.id);
          setEvents((audit.events ?? []) as AuditEvent[]);
          setRunItems(audit.items ?? []);
          notify("远端对账已完成。", "ok");
        }
        return;
      }
      if (["FAILED", "INTERRUPTED", "STOPPED"].includes(next.state)) {
        notify(next.last_message || "后台任务未完成，可从当前状态重试。", "error");
      }
    },
    backgroundTask?.state === "RUNNING" && Boolean(project),
    { key: backgroundTask?.id ?? "", intervalMs: 1_500 }
  );

  usePolling(
    async () => {
      if (!project) return;
      const result = await api.getScan(project.id);
      if (result.status === "COMPLETED" && result.summary.scan_complete) {
        const [guard, remoteTree] = await Promise.all([
          api.getPreflight(project.id),
          api.getTree(project.id)
        ]);
        setPreflight(guard);
        setTree(remoteTree.length ? remoteTree : result.tree);
        setScan(result);
        notify("盘点完成。请检查目录规模与问题清单，再进入预检。");
        return;
      }
      setScan(result);
      if (result.status === "FAILED") {
        notify("盘点未完成，请查看本地完整性检查和日志后重试。", "error");
      }
    },
    Boolean(project) && (scan?.status === "RUNNING" || scan?.status === "PENDING"),
    { key: `${project?.id ?? ""}:${scan?.status ?? ""}`, intervalMs: 1_500 }
  );

  // ------------------------------------------------------------ commands
  const loadTreeChildren = useCallback(
    async (node: TreeNode) => {
      if (!project || node.loading || node.children !== undefined) return;
      setTree((current) => updateTreeNode(current, node.relative_path, { loading: true }));
      try {
        const children = await api.getTree(project.id, node.relative_path);
        setTree((current) =>
          updateTreeNode(current, node.relative_path, { loading: false, children })
        );
      } catch (error) {
        setTree((current) => updateTreeNode(current, node.relative_path, { loading: false }));
        showError(error);
      }
    },
    [project, showError]
  );

  const throttleValid = (): boolean =>
    Number.isFinite(settings.upload_qps) && settings.upload_qps > 0 && settings.upload_qps <= 5;

  const saveSettings = () =>
    api.saveSettings({
      app_id: settings.app_id,
      app_secret: secret || undefined,
      redirect_uri: settings.redirect_uri,
      scopes: DEFAULT_SCOPES,
      upload_qps: settings.upload_qps,
      wiki_calls_per_minute: settings.wiki_calls_per_minute,
      daily_upload_budget: 0
    });

  const validateApp = async (): Promise<boolean> => {
    markValidation("app", "checking", "正在由后端校验并安全保存应用配置…");
    if (!settings.app_id.trim() || (!settings.app_secret_configured && !secret.trim())) {
      markValidation("app", "failed", "必须填写应用编号；首次配置还必须填写应用密钥");
      return false;
    }
    if (!throttleValid()) {
      markValidation("app", "failed", "后端会原子保存全部设置，请先修正并发速率");
      return false;
    }
    try {
      setSettings(await saveSettings());
      setSecret("");
      setAuth(await api.getAuthStatus());
      const result = await api.verifyApp();
      markValidation("app", "passed", translateTechnicalMessage(result.message));
      return true;
    } catch (error) {
      markValidation(
        "app",
        "failed",
        translateTechnicalMessage(error instanceof Error ? error.message : "应用配置验证失败")
      );
      return false;
    }
  };

  const validateThrottle = async (): Promise<boolean> => {
    markValidation("throttle", "checking", "正在验证云盘上传速率…");
    if (!throttleValid()) {
      markValidation("throttle", "failed", "每秒上传请求数必须大于 0 且不超过 5");
      return false;
    }
    if (!settings.app_id.trim() || (!settings.app_secret_configured && !secret.trim())) {
      markValidation("throttle", "failed", "请先填写飞书应用编号和应用密钥，本机服务才能保存设置");
      return false;
    }
    try {
      const saved = await saveSettings();
      setSettings(saved);
      setSecret("");
      markValidation(
        "throttle",
        "passed",
        `每秒最多发起 ${saved.upload_qps} 次上传请求；触发飞书平台限流时自动冷却重试`
      );
      return true;
    } catch (error) {
      markValidation(
        "throttle",
        "failed",
        error instanceof Error ? error.message : "并发速率保存失败"
      );
      return false;
    }
  };

  const validateOauth = async (): Promise<boolean> => {
    markValidation("oauth", "checking", "正在回读用户授权身份与权限范围…");
    try {
      const result = await api.verifyOauth();
      setAuth(await api.getAuthStatus());
      markValidation("oauth", "passed", translateTechnicalMessage(result.message));
      return true;
    } catch (error) {
      markValidation(
        "oauth",
        "failed",
        translateTechnicalMessage(error instanceof Error ? error.message : "用户授权身份验证失败")
      );
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

  const persistProject = async (silent = false): Promise<Project | undefined> => {
    if (!draft.source_root.trim() || !draft.target_wiki_url.trim()) {
      if (!silent) notify("请填写本地源目录和飞书云盘目标文件夹地址。", "warning");
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
    markValidation("source", "checking", "正在检查本机根目录配置…");
    if (!isValidSourceRoot(draft.source_root)) {
      markValidation("source", "failed", "请输入盘符绝对路径或网络共享路径");
      return false;
    }
    try {
      const result = await api.verifySource(draft.source_root.trim());
      markValidation("source", "passed", translateTechnicalMessage(result.message));
      if (isValidDriveFolderUrl(draft.target_wiki_url)) await persistProject(true);
      return true;
    } catch (error) {
      markValidation(
        "source",
        "failed",
        error instanceof Error
          ? translateTechnicalMessage(error.message)
          : "本地根目录验证失败"
      );
      return false;
    }
  };

  const validateTarget = async (): Promise<boolean> => {
    markValidation("target", "checking", "正在检查飞书云盘目标文件夹…");
    if (!isValidDriveFolderUrl(draft.target_wiki_url)) {
      markValidation("target", "failed", "必须填写有效的飞书云盘文件夹地址");
      return false;
    }
    try {
      const result = await api.verifyTarget(draft.target_wiki_url.trim());
      if (isValidSourceRoot(draft.source_root)) await persistProject(true);
      markValidation("target", "passed", translateTechnicalMessage(result.message));
      return true;
    } catch (error) {
      markValidation(
        "target",
        "failed",
        error instanceof Error
          ? translateTechnicalMessage(error.message)
          : "本机服务未接受云盘目标文件夹配置"
      );
      return false;
    }
  };

  const validatePolicy = async (): Promise<boolean> => {
    markValidation("policy", "checking", "正在检查根节点与安全增量策略…");
    if (!draft.create_wrapper) {
      markValidation("policy", "failed", "云盘迁移必须创建同名根文件夹");
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
      if (sourceOk && targetOk && policyOk) {
        try {
          projectOk = Boolean(await persistProject(true));
        } catch (error) {
          const message = error instanceof Error ? error.message : "项目配置保存失败";
          markValidation("source", "failed", message);
          markValidation("target", "failed", message);
          projectOk = false;
        }
      }
      const allPassed =
        appOk && throttleOk && oauthOk && sourceOk && targetOk && policyOk && projectOk;
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

  const configReady =
    Boolean(project) && Object.values(validation).every((item) => item.status === "passed");
  const scanActive = busy === "scan" || scan?.status === "RUNNING" || scan?.status === "PENDING";

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
      setScan(await api.getScan(saved.id));
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
      notify(
        result.writable ? "预检通过，可以生成差异计划。" : "预检完成，仍有阻断项。",
        result.writable ? "ok" : "warning"
      );
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
      if ("counts" in result) {
        setPlan(result);
        setStep("plan");
      } else {
        setBackgroundTask(result);
        notify("差异计划任务已受理，页面会持续显示处理进度。", "info");
      }
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
      if (!("counts" in result)) throw new Error("确认计划返回了意外的任务状态");
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
      const runId = result.id || (result as RunSummary & { run_id?: string }).run_id;
      if (!runId) throw new Error("服务未返回迁移任务 ID");
      setRun(result.id ? result : await api.getRun(runId));
      setProject({ ...project, last_run_id: runId });
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
      setRun(await api.controlRun(run.id, action));
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

  const stopBackgroundTask = async () => {
    if (!backgroundTask) return;
    try {
      setBackgroundTask(await api.controlRun(backgroundTask.id, "stop"));
    } catch (error) {
      showError(error);
    }
  };

  const reconcile = async () => {
    if (!project) return;
    setBusy("reconcile");
    try {
      const result = await api.reconcile(project.id);
      if ("id" in result) {
        setBackgroundTask(result);
        notify("远端对账任务已受理，完成后会自动刷新结果。", "info");
      } else {
        const legacy = result as unknown as { matched: number; conflicts: number };
        notify(
          `远端对账完成：匹配 ${legacy.matched ?? 0}，冲突 ${legacy.conflicts ?? 0}。`,
          legacy.conflicts ? "warning" : "ok"
        );
      }
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
      downloadBlob(
        file,
        `Folder2Feishu_${project.name}_${new Date().toISOString().slice(0, 10)}.${format}`
      );
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
      const activeProject = projects.find((item) => item.id === project?.id) ?? projects[0];
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

  // ------------------------------------------------------------- derived
  const stepStatus = (id: StepId): StepStatus => {
    if (id === step) return "active";
    if (id === "config") return configReady ? "done" : "pending";
    if (id === "scan") return scan?.summary.scan_complete ? "done" : "pending";
    if (id === "preflight") return preflight?.writable ? "done" : preflight ? "blocked" : "pending";
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

  const blocking = preflight?.checks.filter((check) => check.blocking) ?? [];
  const progress = run ? formatPercent(run.completed, run.total) : 0;
  const byteProgress = run ? formatPercent(run.bytes_completed, run.bytes_total) : 0;
  const runProcessed = run ? run.completed + run.failed + run.conflicts : 0;
  const passedCount = Object.values(validation).filter((item) => item.status === "passed").length;

  const filteredActions = useMemo(
    () =>
      plan?.actions.filter((action) => actionFilter === "ALL" || action.kind === actionFilter) ?? [],
    [actionFilter, plan]
  );

  const filteredRunItems = useMemo(
    () =>
      runItems.filter((item) => {
        if (runFilter === "FAILED") return ATTENTION_ITEM_STATUSES.includes(item.status);
        if (runFilter === "ACTIVE") return ACTIVE_ITEM_STATUSES.includes(item.status);
        return true;
      }),
    [runFilter, runItems]
  );

  return {
    // state
    step, setStep, version, settings, setSettings, secret, setSecret, auth, project, draft,
    setDraft, scan, preflight, tree, plan, run, backgroundTask, runItems, events, runtimeLogs,
    validation, actionFilter, setActionFilter, runFilter, setRunFilter, busy, booting, bootError,
    notice, setNotice,
    // derived
    configReady, scanActive, blocking, progress, byteProgress, runProcessed, passedCount,
    filteredActions, filteredRunItems, stepStatus, stepEnabled, stepDisabledReason,
    // commands
    notify, showError, markValidation, invalidateDownstream, loadTreeChildren, validateApp,
    validateThrottle, validateOauth, validateSource, validateTarget, validatePolicy, validateAll,
    beginAuth, startScan, refreshPreflight, buildPlan, confirmPlan, startRun, controlRun,
    stopBackgroundTask, reconcile, exportAudit, refreshCurrentPage
  };
}

export type MigrationConsole = ReturnType<typeof useMigrationConsole>;
