import { ApiError } from "../api/client";
import type { AppSettings, ProjectDraft, TreeNode } from "../types";
import { DEFAULT_SCOPES } from "./labels";

export const BOOT_TIMEOUT_MS = 15_000;

export async function withBootTimeout<T>(promise: Promise<T>): Promise<T> {
  let timer: number | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = window.setTimeout(
      () => reject(new ApiError("本机服务响应超时，请重新连接或查看运行日志。")),
      BOOT_TIMEOUT_MS
    );
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    if (timer !== undefined) window.clearTimeout(timer);
  }
}

export const emptySettings: AppSettings = {
  app_id: "",
  redirect_uri: "http://127.0.0.1:8000/oauth/callback",
  scopes: DEFAULT_SCOPES,
  app_secret_configured: false,
  upload_qps: 5,
  wiki_calls_per_minute: 100,
  daily_upload_budget: 0
};

export const emptyDraft: ProjectDraft = {
  name: "JF 文档迁移",
  source_root: "",
  target_wiki_url: "",
  create_wrapper: true,
  wrapper_name: ""
};

export type ValidationKey = "app" | "oauth" | "throttle" | "source" | "target" | "policy";
export type ValidationStatus = "idle" | "checking" | "passed" | "failed";
export type ValidationEntry = { status: ValidationStatus; message: string };
export type ValidationState = Record<ValidationKey, ValidationEntry>;

export const emptyValidation: ValidationState = {
  app: { status: "idle", message: "尚未验证应用配置" },
  oauth: { status: "idle", message: "尚未验证固定操作身份" },
  throttle: { status: "idle", message: "尚未验证云盘上传速率" },
  source: { status: "idle", message: "尚未检查本地根目录配置" },
  target: { status: "idle", message: "尚未检查云盘目标文件夹" },
  policy: { status: "idle", message: "尚未检查增量策略" }
};

export const validationChecklist: Array<[ValidationKey, string, string, string]> = [
  ["app", "01", "飞书应用", "应用编号、应用密钥与回调地址"],
  ["oauth", "02", "用户授权", "固定用户与完整权限范围"],
  ["throttle", "03", "上传速率", "云盘写入与限流保护"],
  ["source", "04", "本地来源", "本机绝对路径"],
  ["target", "05", "云盘目标", "目标文件夹地址"],
  ["policy", "06", "安全策略", "根节点与安全增量"]
];

export function updateTreeNode(
  nodes: TreeNode[],
  relativePath: string,
  changes: Partial<TreeNode>
): TreeNode[] {
  return nodes.map((node) =>
    node.relative_path === relativePath
      ? { ...node, ...changes }
      : node.children
        ? { ...node, children: updateTreeNode(node.children, relativePath, changes) }
        : node
  );
}

/** Windows absolute path (C:\...) or UNC share (\\server\share). */
export const isValidSourceRoot = (value: string): boolean =>
  /^[A-Za-z]:\\/.test(value.trim()) || /^\\\\[^\\]+\\[^\\]+/.test(value.trim());

export const isValidDriveFolderUrl = (value: string): boolean => {
  try {
    const target = new URL(value.trim());
    return (
      target.protocol === "https:" &&
      (target.hostname.endsWith(".feishu.cn") || target.hostname.endsWith(".larksuite.com")) &&
      /^\/drive\/folder\/[A-Za-z0-9]+/.test(target.pathname)
    );
  } catch {
    return false;
  }
};
